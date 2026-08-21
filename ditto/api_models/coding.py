"""Shadow-only DittoBench Coding contract v1 models.

These models define the known-field projection used by the future scorer and
validator.  They do not activate a benchmark version or make coding evidence
weight eligible.
"""

from __future__ import annotations

import hashlib
import ipaddress
import json
import posixpath
import re
import unicodedata
from datetime import UTC, datetime, timedelta
from enum import StrEnum
from typing import Annotated, Any, Literal
from urllib.parse import parse_qs, urlparse
from uuid import UUID

from pydantic import (
    AfterValidator,
    BaseModel,
    ConfigDict,
    Field,
    HttpUrl,
    TypeAdapter,
    field_validator,
    model_validator,
)

CODING_CONTRACT_VERSION = 1
MAX_CANONICAL_JSON_BYTES = 4 << 20
REPAIR_SCORE_RESOLVED_MICROS = 1_000_000
UINT32_MAX = (1 << 32) - 1
UINT64_MAX = (1 << 64) - 1

_SHA256_PATTERN = r"^[0-9a-f]{64}$"
_OCI_DIGEST_PATTERN = r"^sha256:[0-9a-f]{64}$"
_BLOCK_HASH_PATTERN = r"^0x[0-9a-f]{64}$"
_CONTENT_KEY_PATTERN = r"^sha256/[0-9a-f]{64}$"
_SS58_PATTERN = r"^[1-9A-HJ-NP-Za-km-z]{47,48}$"
_SIGNATURE_HEX_PATTERN = r"^[0-9a-fA-F]{128}$"
_ARTIFACT_RFC3339 = re.compile(
    r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d{1,6})?(?:Z|[+-]\d{2}:\d{2})$"
)
_REQUIRED_TEST_GROUPS = frozenset(
    {"adversarial", "fail_to_pass", "hidden", "integrity", "pass_to_pass"}
)
_SORTED_TEST_GROUPS = (
    "adversarial",
    "fail_to_pass",
    "hidden",
    "integrity",
    "pass_to_pass",
)
_GRADER_EXECUTION_ORDER = (
    "fail_to_pass",
    "pass_to_pass",
    "hidden",
    "adversarial",
    "integrity",
)
_INITIAL_GRADER_RECEIPT_ROOT = "0" * 64


def _validate_identifier(value: str, max_bytes: int) -> str:
    if len(value.encode()) > max_bytes:
        raise ValueError(f"identifier must contain at most {max_bytes} UTF-8 bytes")
    if any(
        character.isspace() or unicodedata.category(character) == "Cc"
        for character in value
    ):
        raise ValueError("identifier must not contain whitespace or control characters")
    return value


def _validate_opaque_id(value: str) -> str:
    return _validate_identifier(value, 256)


def _validate_short_name(value: str) -> str:
    return _validate_identifier(value, 128)


def _validate_command_id(value: str) -> str:
    return _validate_identifier(value, 80)


def _validate_url(value: str) -> str:
    if len(value.encode()) > 4096:
        raise ValueError("capability URL exceeds 4096 UTF-8 bytes")
    parsed = TypeAdapter(HttpUrl).validate_python(value)
    if parsed.username is not None or parsed.password is not None or parsed.fragment:
        raise ValueError("capability URL must not contain credentials or a fragment")
    return value


def _validate_relative_path(value: str) -> str:
    if (
        len(value.encode()) > 256
        or value.startswith("/")
        or "\\" in value
        or any(part in {"", ".", "..", ".git"} for part in value.split("/"))
        or any(unicodedata.category(character) == "Cc" for character in value)
    ):
        raise ValueError("editable path must be a safe workspace-relative POSIX path")
    return value


OpaqueId = Annotated[
    str,
    Field(min_length=1, max_length=256),
    AfterValidator(_validate_opaque_id),
]
ShortName = Annotated[
    str,
    Field(min_length=1, max_length=128),
    AfterValidator(_validate_short_name),
]
CommandId = Annotated[
    str,
    Field(min_length=1, max_length=80),
    AfterValidator(_validate_command_id),
]
Sha256 = Annotated[str, Field(pattern=_SHA256_PATTERN)]
OciDigest = Annotated[str, Field(pattern=_OCI_DIGEST_PATTERN)]
BlockHash = Annotated[str, Field(pattern=_BLOCK_HASH_PATTERN)]
SafeRelativePath = Annotated[
    str,
    Field(min_length=1, max_length=256),
    AfterValidator(_validate_relative_path),
]
CapabilityUrl = Annotated[
    str,
    Field(min_length=1, max_length=4096),
    AfterValidator(_validate_url),
]


class CodingContractModel(BaseModel):
    """Immutable forward-compatible wire model."""

    model_config = ConfigDict(
        extra="ignore",
        frozen=True,
        strict=True,
        serialize_by_alias=True,
        validate_by_name=True,
    )


class CodingTerminalDomain(StrEnum):
    """Mutually exclusive authoritative outcome for one selected task."""

    RESOLVED = "resolved"
    REPAIR_FAILURE = "repair_failure"
    VALIDATOR_INFRASTRUCTURE = "validator_infrastructure"
    TASK_INVALID = "task_invalid"
    CANDIDATE_INTEGRITY = "candidate_integrity"
    CONTROL_PLANE_INTEGRITY = "control_plane_integrity"


class CodingModelUsageStatus(StrEnum):
    """Authoritative provider-use state, including attributable zero-use."""

    COMPLETE = "complete"
    NOT_INVOKED = "not_invoked"
    PROVIDER_FAILURE = "provider_failure"


class CodingCertificationStatus(StrEnum):
    """Shadow capability result; it is never a reward score."""

    UNSUPPORTED = "unsupported"
    FAILED = "failed"
    CERTIFIED = "certified"


class CodingCertificationStage(StrEnum):
    """Last candidate-attributable certification transition."""

    HEALTH = "health"
    SEED = "seed"
    RUN = "run"
    FREEZE = "freeze"
    GRADE = "grade"


class CodingManifestTask(CodingContractModel):
    case_id: OpaqueId
    variant_id: OpaqueId
    profile_capability_id: OpaqueId
    visible_bundle_sha256: Sha256
    base_tree_sha256: Sha256
    memory_bundle_sha256: Sha256
    environment_image_digest: OciDigest
    environment_platform: Literal["linux/amd64"]
    resource_profile_sha256: Sha256
    grader_bundle_sha256: Sha256
    grader_image_digest: OciDigest
    grader_platform: Literal["linux/amd64"]
    test_manifest_sha256: Sha256
    grader_plan_sha256: Sha256


class CodingGraderCommand(CodingContractModel):
    id: CommandId
    argv: Annotated[list[str], Field(min_length=1, max_length=64)]
    timeout_milliseconds: Annotated[int, Field(ge=1, le=600_000)]

    @model_validator(mode="after")
    def command_is_bounded(self) -> CodingGraderCommand:
        executable = self.argv[0]
        if (
            len(executable.encode()) > 128
            or "/" in executable
            or "\\" in executable
            or any(
                character.isspace() or unicodedata.category(character) == "Cc"
                for character in executable
            )
            or executable.casefold()
            in {"bash", "cmd", "dash", "env", "fish", "powershell", "pwsh", "sh", "zsh"}
        ):
            raise ValueError("grader command executable is outside contract bounds")
        for argument in self.argv[1:]:
            if not argument or len(argument.encode()) > 4096 or "\x00" in argument:
                raise ValueError("grader command argument is outside contract bounds")
        return self


class CodingGraderTestGroupPlan(CodingContractModel):
    group: Literal["fail_to_pass", "pass_to_pass", "hidden", "adversarial", "integrity"]
    command: CodingGraderCommand
    expected_total: Annotated[int, Field(ge=1, le=UINT32_MAX)]


class CodingGraderPlan(CodingContractModel):
    schema_name: Literal["dittobench-coding-grader-plan-v1"] = Field(alias="schema")
    coding_contract_version: Literal[1]
    case_id: OpaqueId
    variant_id: OpaqueId
    visible_bundle_sha256: Sha256
    base_tree_sha256: Sha256
    grader_contract_sha256: Sha256
    grader_bundle_sha256: Sha256
    grader_image_digest: OciDigest
    grader_platform: Literal["linux/amd64"]
    test_manifest_sha256: Sha256
    resource_profile_sha256: Sha256
    execution_timeout_milliseconds: Annotated[int, Field(ge=1, le=3_600_000)]
    build_required: bool
    build_command: CodingGraderCommand
    test_groups: Annotated[
        list[CodingGraderTestGroupPlan], Field(min_length=5, max_length=5)
    ]
    execution_order: Annotated[list[str], Field(min_length=5, max_length=5)]

    @model_validator(mode="after")
    def plan_is_canonical(self) -> CodingGraderPlan:
        groups = tuple(group.group for group in self.test_groups)
        if (
            groups != _SORTED_TEST_GROUPS
            or tuple(self.execution_order) != _GRADER_EXECUTION_ORDER
        ):
            raise ValueError("grader groups or execution order are not canonical")
        command_ids = [self.build_command.id] + [
            group.command.id for group in self.test_groups
        ]
        if len(command_ids) != len(set(command_ids)):
            raise ValueError("grader command IDs must be unique")
        return self


class CodingGraderLimits(CodingContractModel):
    max_bundle_bytes: Annotated[int, Field(ge=1, le=2 << 30)]
    max_workspace_bytes: Annotated[int, Field(ge=1, le=4 << 30)]
    max_file_bytes: Annotated[int, Field(ge=1, le=128 << 20)]
    max_patch_bytes: Annotated[int, Field(ge=1, le=128 << 20)]
    max_entries: Annotated[int, Field(ge=1, le=200_000)]
    max_tool_calls: Annotated[int, Field(ge=1, le=1_000)]
    max_read_bytes: Annotated[int, Field(ge=1, le=256 << 10)]
    max_response_bytes: Annotated[int, Field(ge=4096, le=2 << 20)]
    max_search_results: Annotated[int, Field(ge=1, le=1_000)]
    max_replay_cache_bytes: Annotated[int, Field(ge=1, le=512 << 20)]
    max_transcript_bytes: Annotated[int, Field(ge=1, le=512 << 20)]

    @model_validator(mode="after")
    def aggregate_limits_are_coherent(self) -> CodingGraderLimits:
        if (
            self.max_file_bytes > self.max_workspace_bytes
            or self.max_patch_bytes > self.max_workspace_bytes
        ):
            raise ValueError("grader file or patch limit exceeds workspace limit")
        if self.max_read_bytes > self.max_response_bytes - 2048:
            raise ValueError("grader read limit exceeds response limit")
        if self.max_tool_calls * self.max_response_bytes > self.max_replay_cache_bytes:
            raise ValueError("grader replay cache cannot retain every response")
        max_event_bytes = 2 * (64 << 10) + self.max_response_bytes + 8192
        if self.max_tool_calls * max_event_bytes > self.max_transcript_bytes:
            raise ValueError("grader transcript cannot retain every event")
        return self


class CodingGraderResourceProfile(CodingContractModel):
    schema_name: Literal["dittobench-coding-grader-resource-v1"] = Field(alias="schema")
    candidate_limits: CodingGraderLimits
    protected_limits: CodingGraderLimits
    max_combined_disk_bytes: Annotated[int, Field(ge=1, le=8 << 30)]
    memory_limit_bytes: Annotated[int, Field(ge=256 << 20, le=64 << 30)]
    scratch_limit_bytes: Annotated[int, Field(ge=1, le=8 << 30)]
    pids_limit: Annotated[int, Field(ge=1, le=4096)]
    cpu_quota_millis: Annotated[int, Field(ge=100, le=64_000)]

    @model_validator(mode="after")
    def combined_disk_is_bounded(self) -> CodingGraderResourceProfile:
        peak = (
            self.candidate_limits.max_workspace_bytes
            + self.protected_limits.max_workspace_bytes
            + max(
                self.candidate_limits.max_bundle_bytes,
                self.protected_limits.max_bundle_bytes,
            )
            + self.scratch_limit_bytes
        )
        if peak > self.max_combined_disk_bytes:
            raise ValueError("grader combined disk peak exceeds its ceiling")
        return self


class CodingGraderExecutionReceipt(CodingContractModel):
    schema_name: Literal["dittobench-coding-grader-receipt-v1"] = Field(alias="schema")
    sequence: Annotated[int, Field(ge=1, le=6)]
    phase: Literal["build", "test"]
    group: (
        Literal["fail_to_pass", "pass_to_pass", "hidden", "adversarial", "integrity"]
        | None
    )
    command_id: CommandId
    command_sha256: Sha256
    executor_instance_id: OpaqueId
    returncode: Annotated[int, Field(ge=-(1 << 63), le=(1 << 63) - 1)]
    passed: Annotated[int, Field(ge=0, le=UINT32_MAX)]
    total: Annotated[int, Field(ge=0, le=UINT32_MAX)]
    completed: bool
    timed_out: bool
    previous_receipt_sha256: Sha256

    @model_validator(mode="after")
    def phase_is_coherent(self) -> CodingGraderExecutionReceipt:
        if self.phase == "build" and (
            self.group is not None or self.passed != 0 or self.total != 0
        ):
            raise ValueError("build receipt contains test fields")
        if self.phase == "test" and (
            self.group is None or self.total == 0 or self.passed > self.total
        ):
            raise ValueError("test receipt is incoherent")
        return self


class CodingRunManifest(CodingContractModel):
    schema_name: Literal["dittobench-coding-run-manifest-v1"] = Field(alias="schema")
    coding_contract_version: Literal[1]
    bench_family: Literal["coding"]
    weight_eligible: Literal[False]
    coding_run_id: OpaqueId
    agent_id: OpaqueId
    agent_artifact_sha256: Sha256
    corpus_release_id: OpaqueId
    catalog_merkle_root: Sha256
    selection_derivation_id: ShortName
    selection_chain_genesis_hash: BlockHash
    selection_block_number: Annotated[int, Field(ge=1, le=UINT64_MAX)]
    selection_block_hash: BlockHash
    inference_grant_sha256: Sha256
    grader_contract_sha256: Sha256
    task_set_id: OpaqueId
    task_set_manifest_sha256: Sha256
    tasks: Annotated[list[CodingManifestTask], Field(min_length=1, max_length=100)]

    @model_validator(mode="after")
    def tasks_are_unique_and_sorted(self) -> CodingRunManifest:
        identities = [(task.case_id, task.variant_id) for task in self.tasks]
        if identities != sorted(identities) or len(set(identities)) != len(identities):
            raise ValueError(
                "manifest tasks must be unique and sorted by case_id, variant_id"
            )
        return self


class CodingVisibleMemory(CodingContractModel):
    memory_id: OpaqueId
    repository_capability_id: OpaqueId | None
    fact_group_id: OpaqueId | None
    scope: ShortName
    type: ShortName
    content: Annotated[str, Field(min_length=1, max_length=16 * 1024)]
    valid_from_epoch: OpaqueId | None
    valid_until_epoch: OpaqueId | None
    supersedes: Annotated[list[OpaqueId], Field(max_length=64)]
    confidence_micros: Annotated[int, Field(ge=0, le=1_000_000)]

    @field_validator("content")
    @classmethod
    def content_fits_byte_bound(cls, value: str) -> str:
        if len(value.encode()) > 16 * 1024:
            raise ValueError("memory content exceeds 16384 UTF-8 bytes")
        return value

    @model_validator(mode="after")
    def supersession_is_canonical(self) -> CodingVisibleMemory:
        if sorted(self.supersedes) != self.supersedes:
            raise ValueError("supersedes must be unique and sorted")
        if len(set(self.supersedes)) != len(self.supersedes):
            raise ValueError("supersedes must be unique and sorted")
        if self.memory_id in self.supersedes:
            raise ValueError("memory cannot supersede itself")
        return self


class CodingSeedRequest(CodingContractModel):
    coding_contract_version: Literal[1]
    ticket_id: OpaqueId
    case_id: OpaqueId
    profile_capability_id: OpaqueId
    memory_bundle_sha256: Sha256
    memories: Annotated[list[CodingVisibleMemory], Field(max_length=128)]

    @model_validator(mode="after")
    def memory_bundle_is_canonical(self) -> CodingSeedRequest:
        identities = [memory.memory_id for memory in self.memories]
        if identities != sorted(identities) or len(set(identities)) != len(identities):
            raise ValueError("memories must be unique and sorted by memory_id")
        if memory_bundle_digest(self.memories) != self.memory_bundle_sha256:
            raise ValueError("memory_bundle_sha256 does not match canonical memories")
        return self


class CodingIssue(CodingContractModel):
    title: Annotated[str, Field(max_length=1024)]
    description: Annotated[str, Field(min_length=1, max_length=64 * 1024)]
    constraints: Annotated[list[str], Field(max_length=64)]

    @field_validator("constraints")
    @classmethod
    def validate_constraints(cls, values: list[str]) -> list[str]:
        if any(not value or len(value.encode()) > 4096 for value in values):
            raise ValueError("constraints must contain 1..=4096 UTF-8 bytes")
        return values

    @model_validator(mode="after")
    def text_fits_byte_bounds(self) -> CodingIssue:
        if (
            len(self.title.encode()) > 1024
            or len(self.description.encode()) > 64 * 1024
        ):
            raise ValueError("issue title or description exceeds its UTF-8 byte bound")
        return self


class CodingRuntimePolicy(CodingContractModel):
    editable_paths: Annotated[list[SafeRelativePath], Field(max_length=64)]
    test_command_ids: Annotated[list[CommandId], Field(max_length=64)]
    build_command_ids: Annotated[list[CommandId], Field(max_length=64)]

    @model_validator(mode="after")
    def collections_are_unique(self) -> CodingRuntimePolicy:
        for label, values in (
            ("editable_paths", self.editable_paths),
            ("test_command_ids", self.test_command_ids),
            ("build_command_ids", self.build_command_ids),
        ):
            if len(set(values)) != len(values):
                raise ValueError(f"{label} must not contain duplicates")
        return self


class CodingBudgets(CodingContractModel):
    model_input_tokens: Annotated[int, Field(ge=1, le=2_000_000)]
    model_output_tokens: Annotated[int, Field(ge=1, le=250_000)]
    workspace_tool_calls: Annotated[int, Field(ge=1, le=1_000)]
    wall_time_seconds: Annotated[int, Field(ge=1, le=7_200)]


class CodingRunRequest(CodingContractModel):
    coding_contract_version: Literal[1]
    ticket_id: OpaqueId
    case_id: OpaqueId
    profile_capability_id: OpaqueId
    repository_epoch: OpaqueId
    visible_bundle_sha256: Sha256
    issue: CodingIssue
    runtime_policy: CodingRuntimePolicy
    workspace_capability_url: CapabilityUrl
    inference_base_url: CapabilityUrl
    budgets: CodingBudgets


class CodingModelEvidence(CodingContractModel):
    model: ShortName
    provider: ShortName
    provider_route_profile: ShortName
    reasoning_effort: Literal["medium"]
    inference_grant_sha256: Sha256
    prompt_sha256: Sha256
    tool_schema_sha256: Sha256
    usage_status: CodingModelUsageStatus
    fallback_used: Literal[False]
    cost_source: Literal["provider_receipt_v1"]
    currency: Literal["USD"]
    provider_receipt_set_sha256: Sha256 | None
    requests: Annotated[int, Field(ge=0, le=10_000)]
    prompt_tokens: Annotated[int, Field(ge=0, le=UINT64_MAX)]
    completion_tokens: Annotated[int, Field(ge=0, le=UINT64_MAX)]
    total_tokens: Annotated[int, Field(ge=0, le=UINT64_MAX)]
    cost_usd_micros: Annotated[int, Field(ge=0, le=UINT64_MAX)]
    retry_count: Annotated[int, Field(ge=0, le=100)]

    @model_validator(mode="after")
    def token_totals_are_coherent(self) -> CodingModelEvidence:
        if self.total_tokens != self.prompt_tokens + self.completion_tokens:
            raise ValueError(
                "total_tokens must equal prompt_tokens + completion_tokens"
            )
        counters = (
            self.requests,
            self.prompt_tokens,
            self.completion_tokens,
            self.total_tokens,
            self.cost_usd_micros,
            self.retry_count,
        )
        if self.usage_status is CodingModelUsageStatus.NOT_INVOKED:
            if any(counters) or self.provider_receipt_set_sha256 is not None:
                raise ValueError(
                    "not_invoked model evidence requires canonical zero accounting"
                )
        elif self.requests == 0 or self.provider_receipt_set_sha256 is None:
            raise ValueError(
                "invoked model evidence requires requests and a provider receipt root"
            )
        return self


class CodingAuthoringEvidence(CodingContractModel):
    model: CodingModelEvidence
    authoring_event_root: Sha256
    authoring_transcript_sha256: Sha256
    frozen_patch_sha256: Sha256
    changed_path_root: Sha256
    final_tree_sha256: Sha256
    changed_path_count: Annotated[int, Field(ge=0, le=10_000)]
    changed_bytes: Annotated[int, Field(ge=0, le=1 << 30)]
    protected_paths_intact: bool


class CodingBuildEvidence(CodingContractModel):
    command_id: CommandId
    required: bool
    passed: bool


class CodingTestGroupEvidence(CodingContractModel):
    group: Literal["fail_to_pass", "pass_to_pass", "hidden", "adversarial", "integrity"]
    passed: Annotated[int, Field(ge=0, le=UINT32_MAX)]
    total: Annotated[int, Field(ge=1, le=UINT32_MAX)]

    @model_validator(mode="after")
    def passed_does_not_exceed_total(self) -> CodingTestGroupEvidence:
        if self.passed > self.total:
            raise ValueError("passed test count cannot exceed total")
        return self


class CodingGraderEvidence(CodingContractModel):
    grader_contract_sha256: Sha256
    grader_bundle_sha256: Sha256
    grader_image_digest: OciDigest
    grader_platform: Literal["linux/amd64"]
    test_manifest_sha256: Sha256
    grader_plan_sha256: Sha256
    resource_profile_sha256: Sha256
    execution_receipt_root_sha256: Sha256
    execution_receipt_count: Annotated[int, Field(ge=0, le=6)]
    grader_integrity_before_sha256: Sha256
    grader_integrity_after_sha256: Sha256
    build: CodingBuildEvidence
    test_groups: Annotated[
        list[CodingTestGroupEvidence], Field(min_length=5, max_length=5)
    ]

    @model_validator(mode="after")
    def test_groups_are_complete_and_sorted(self) -> CodingGraderEvidence:
        names = [group.group for group in self.test_groups]
        if names != sorted(names) or set(names) != _REQUIRED_TEST_GROUPS:
            raise ValueError("grader test groups must be complete, unique, and sorted")
        return self

    def resolved(self) -> bool:
        expected_receipts = 5 + int(self.build.required)
        return (
            (not self.build.required or self.build.passed)
            and all(group.passed == group.total for group in self.test_groups)
            and self.grader_integrity_before_sha256
            == self.grader_integrity_after_sha256
            and self.execution_receipt_count == expected_receipts
        )


class CodingTaskEvidence(CodingContractModel):
    schema_name: Literal["dittobench-coding-task-evidence-v1"] = Field(alias="schema")
    coding_contract_version: Literal[1]
    weight_eligible: Literal[False]
    coding_run_id: OpaqueId
    validator_ticket_id: OpaqueId
    agent_id: OpaqueId
    agent_artifact_sha256: Sha256
    corpus_release_id: OpaqueId
    task_set_id: OpaqueId
    task_set_manifest_sha256: Sha256
    task: CodingManifestTask
    authoring: CodingAuthoringEvidence | None
    grader: CodingGraderEvidence | None
    terminal_domain: CodingTerminalDomain
    failure_code: ShortName | None
    repair_score_micros: Annotated[int, Field(ge=0, le=REPAIR_SCORE_RESOLVED_MICROS)]

    @model_validator(mode="after")
    def outcome_is_coherent(self) -> CodingTaskEvidence:
        if self.grader is not None:
            if self.grader.grader_bundle_sha256 != self.task.grader_bundle_sha256:
                raise ValueError("grader bundle does not match manifest task")
            if self.grader.grader_image_digest != self.task.grader_image_digest:
                raise ValueError("grader image does not match manifest task")
            if self.grader.grader_platform != self.task.grader_platform:
                raise ValueError("grader platform does not match manifest task")
            if self.grader.test_manifest_sha256 != self.task.test_manifest_sha256:
                raise ValueError("test manifest does not match manifest task")
            if self.grader.grader_plan_sha256 != self.task.grader_plan_sha256:
                raise ValueError("grader plan does not match manifest task")
            if self.grader.resource_profile_sha256 != self.task.resource_profile_sha256:
                raise ValueError("grader resource profile does not match manifest task")

        if self.terminal_domain is CodingTerminalDomain.RESOLVED:
            if (
                self.failure_code is not None
                or self.authoring is None
                or self.grader is None
                or not self.authoring.protected_paths_intact
                or not self.grader.resolved()
                or self.repair_score_micros != REPAIR_SCORE_RESOLVED_MICROS
            ):
                raise ValueError(
                    "resolved evidence must contain a complete passing repair"
                )
        elif self.failure_code is None or self.repair_score_micros != 0:
            raise ValueError(
                "non-resolved evidence requires a failure_code and zero score"
            )

        if (
            self.terminal_domain
            in {
                CodingTerminalDomain.REPAIR_FAILURE,
                CodingTerminalDomain.CANDIDATE_INTEGRITY,
            }
            and self.authoring is None
        ):
            raise ValueError(
                "candidate-attributable failure requires authoritative "
                "authoring evidence"
            )
        return self


class CodingTaskResult(CodingContractModel):
    case_id: OpaqueId
    variant_id: OpaqueId
    task_evidence_sha256: Sha256
    terminal_domain: CodingTerminalDomain
    repair_score_micros: Annotated[int, Field(ge=0, le=REPAIR_SCORE_RESOLVED_MICROS)]


class CodingRunEvidence(CodingContractModel):
    schema_name: Literal["dittobench-coding-run-evidence-v1"] = Field(alias="schema")
    coding_contract_version: Literal[1]
    weight_eligible: Literal[False]
    coding_run_id: OpaqueId
    validator_ticket_id: OpaqueId
    run_manifest_sha256: Sha256
    task_set_manifest_sha256: Sha256
    tasks: Annotated[list[CodingTaskResult], Field(min_length=1, max_length=100)]
    resolved_count: Annotated[int, Field(ge=0)]
    repair_failure_count: Annotated[int, Field(ge=0)]
    infrastructure_count: Annotated[int, Field(ge=0)]
    invalid_count: Annotated[int, Field(ge=0)]
    candidate_integrity_count: Annotated[int, Field(ge=0)]
    control_plane_integrity_count: Annotated[int, Field(ge=0)]
    scoreable_task_count: Annotated[int, Field(ge=0)]
    repair_mean_micros: Annotated[int, Field(ge=0, le=REPAIR_SCORE_RESOLVED_MICROS)]

    @model_validator(mode="after")
    def aggregate_is_coherent(self) -> CodingRunEvidence:
        identities = [(task.case_id, task.variant_id) for task in self.tasks]
        if identities != sorted(identities) or len(set(identities)) != len(identities):
            raise ValueError(
                "run tasks must be unique and sorted by case_id, variant_id"
            )

        counts = dict.fromkeys(CodingTerminalDomain, 0)
        score_sum = 0
        for task in self.tasks:
            counts[task.terminal_domain] += 1
            if task.terminal_domain is CodingTerminalDomain.RESOLVED:
                if task.repair_score_micros != REPAIR_SCORE_RESOLVED_MICROS:
                    raise ValueError("resolved task result must score 1000000")
            elif task.repair_score_micros != 0:
                raise ValueError("non-resolved task result must score zero")
            if task.terminal_domain not in {
                CodingTerminalDomain.VALIDATOR_INFRASTRUCTURE,
                CodingTerminalDomain.TASK_INVALID,
                CodingTerminalDomain.CONTROL_PLANE_INTEGRITY,
            }:
                score_sum += task.repair_score_micros

        expected_scoreable = (
            counts[CodingTerminalDomain.RESOLVED]
            + counts[CodingTerminalDomain.REPAIR_FAILURE]
            + counts[CodingTerminalDomain.CANDIDATE_INTEGRITY]
        )
        observed_counts = (
            self.resolved_count,
            self.repair_failure_count,
            self.infrastructure_count,
            self.invalid_count,
            self.candidate_integrity_count,
            self.control_plane_integrity_count,
        )
        expected_counts = (
            counts[CodingTerminalDomain.RESOLVED],
            counts[CodingTerminalDomain.REPAIR_FAILURE],
            counts[CodingTerminalDomain.VALIDATOR_INFRASTRUCTURE],
            counts[CodingTerminalDomain.TASK_INVALID],
            counts[CodingTerminalDomain.CANDIDATE_INTEGRITY],
            counts[CodingTerminalDomain.CONTROL_PLANE_INTEGRITY],
        )
        if (
            observed_counts != expected_counts
            or self.scoreable_task_count != expected_scoreable
        ):
            raise ValueError("run aggregate counts do not match task evidence")
        expected_mean = score_sum // expected_scoreable if expected_scoreable else 0
        if self.repair_mean_micros != expected_mean:
            raise ValueError(
                "repair_mean_micros does not match the scoreable task vector"
            )
        return self


ContentAddressedKey = Annotated[str, Field(pattern=_CONTENT_KEY_PATTERN)]


class CodingCapabilityCertificationReceipt(CodingContractModel):
    """Content-addressed shadow certification emitted by the trusted scorer.

    The digest is integrity-only. A validator submission separately signs the
    agent, screened image, exact ticket lease, and this receipt digest.
    """

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
    model_evidence: CodingModelEvidence | None
    frozen_patch_sha256: Sha256 | None
    frozen_submission_object_key: ContentAddressedKey | None
    changed_path_root: Sha256 | None
    final_tree_sha256: Sha256 | None
    authoring_event_root: Sha256 | None
    authoring_transcript_sha256: Sha256 | None
    authoring_transcript_object_key: ContentAddressedKey | None
    authoring_transcript_bytes: Annotated[int, Field(ge=0)]
    authoring_event_count: Annotated[int, Field(ge=0, le=UINT64_MAX)]
    protected_paths_intact: bool
    canary_terminal_domain: CodingTerminalDomain | None
    grader_plan_sha256: Sha256
    grader_execution_receipt_root_sha256: Sha256 | None
    certification_sha256: Sha256

    @model_validator(mode="after")
    def receipt_is_coherent(self) -> CodingCapabilityCertificationReceipt:
        if (
            self.expires_at_unix <= self.issued_at_unix
            or self.expires_at_unix - self.issued_at_unix > 24 * 60 * 60
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
        if self.canary_terminal_domain not in {
            None,
            CodingTerminalDomain.RESOLVED,
            CodingTerminalDomain.REPAIR_FAILURE,
            CodingTerminalDomain.CANDIDATE_INTEGRITY,
        }:
            raise ValueError("certification carries a non-candidate terminal domain")

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
                is not CodingModelUsageStatus.COMPLETE
                or self.canary_terminal_domain is not CodingTerminalDomain.RESOLVED
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
            raise ValueError("non-certified receipt requires a failure stage and code")
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
    """Validator-signed envelope for one append-only shadow receipt."""

    model_config = ConfigDict(extra="ignore", strict=True)

    validator_hotkey: Annotated[str, Field(pattern=_SS58_PATTERN)]
    bench_version: Annotated[int, Field(ge=1)]
    ticket_deadline: datetime
    screened_image_sha256: Sha256
    receipt: CodingCapabilityCertificationReceipt
    signature: Annotated[str, Field(pattern=_SIGNATURE_HEX_PATTERN)]

    @field_validator("ticket_deadline")
    @classmethod
    def ticket_deadline_is_timezone_aware(cls, value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError(
                "coding certification ticket deadline must be timezone-aware"
            )
        return value


class SubmitCodingCertificationResponse(BaseModel):
    model_config = ConfigDict(extra="ignore", strict=True)

    agent_id: UUID
    certification_id: OpaqueId
    status: CodingCertificationStatus
    accepted: Literal[True]
    idempotent: bool
    active: bool


class CodingArtifactKind(StrEnum):
    VISIBLE_BUNDLE = "visible-bundle"
    MEMORY_BUNDLE = "memory-bundle"
    RESOURCE_PROFILE = "resource-profile"
    GRADER_BUNDLE = "grader-bundle"


class CodingArtifactAudience(StrEnum):
    WORKSPACE_MATERIALIZER = "workspace-materializer"
    MEMORY_SEED_PROJECTOR = "memory-seed-projector"
    RESOURCE_SUPERVISOR = "resource-supervisor"
    PROTECTED_GRADER = "protected-grader"


class CodingArtifactDeliveryPhase(StrEnum):
    AUTHORING = "authoring"
    GRADING = "grading"


_ARTIFACT_POLICY = {
    CodingArtifactKind.VISIBLE_BUNDLE: (
        CodingArtifactAudience.WORKSPACE_MATERIALIZER,
        2 << 30,
        frozenset(
            {
                CodingArtifactDeliveryPhase.AUTHORING,
                CodingArtifactDeliveryPhase.GRADING,
            }
        ),
    ),
    CodingArtifactKind.MEMORY_BUNDLE: (
        CodingArtifactAudience.MEMORY_SEED_PROJECTOR,
        64 << 20,
        frozenset({CodingArtifactDeliveryPhase.AUTHORING}),
    ),
    CodingArtifactKind.RESOURCE_PROFILE: (
        CodingArtifactAudience.RESOURCE_SUPERVISOR,
        4 << 20,
        frozenset(
            {
                CodingArtifactDeliveryPhase.AUTHORING,
                CodingArtifactDeliveryPhase.GRADING,
            }
        ),
    ),
    CodingArtifactKind.GRADER_BUNDLE: (
        CodingArtifactAudience.PROTECTED_GRADER,
        512 << 20,
        frozenset({CodingArtifactDeliveryPhase.GRADING}),
    ),
}


class CodingArtifactCapabilityEnvelope(CodingContractModel):
    schema_name: Literal["dittobench-coding-artifact-capability-v1"] = Field(
        alias="schema"
    )
    coding_contract_version: Literal[1]
    weight_eligible: Literal[False]
    ticket_id: UUID
    ticket_deadline: datetime
    delivery_phase: CodingArtifactDeliveryPhase
    artifact_kind: CodingArtifactKind
    audience: CodingArtifactAudience
    sha256: Sha256
    size_bytes: Annotated[int, Field(ge=1)]
    url: Annotated[str, Field(min_length=1, max_length=16 << 10, repr=False)]
    expires_at: datetime

    @model_validator(mode="after")
    def capability_is_coherent(self) -> CodingArtifactCapabilityEnvelope:
        audience, maximum, phases = _ARTIFACT_POLICY[self.artifact_kind]
        if (
            self.ticket_id.int == 0
            or self.expires_at.microsecond != 0
            or self.expires_at > self.ticket_deadline
            or self.size_bytes > maximum
            or self.audience is not audience
            or self.delivery_phase not in phases
        ):
            raise ValueError("coding artifact capability authority is incoherent")
        _validate_artifact_url(self)
        return self


class CodingAuthoringLeaseRequest(BaseModel):
    model_config = ConfigDict(extra="ignore", strict=True)

    validator_hotkey: Annotated[str, Field(pattern=_SS58_PATTERN)]
    ticket_id: UUID
    nonce: UUID
    requested_at: datetime
    signature: Annotated[str, Field(pattern=_SIGNATURE_HEX_PATTERN)]

    @model_validator(mode="after")
    def request_is_coherent(self) -> CodingAuthoringLeaseRequest:
        if (
            self.ticket_id.int == 0
            or self.nonce.int == 0
            or self.requested_at.tzinfo is None
            or self.requested_at.utcoffset() is None
        ):
            raise ValueError("coding authoring request authority is invalid")
        return self


class CodingAuthoringLeaseResponse(CodingContractModel):
    schema_name: Literal["dittobench-coding-authoring-lease-v1"] = Field(alias="schema")
    coding_contract_version: Literal[1]
    weight_eligible: Literal[False]
    ticket_id: UUID
    ticket_deadline: datetime
    coding_run_id: OpaqueId
    run_manifest_sha256: Sha256
    task_set_manifest_sha256: Sha256
    repository_epoch: OpaqueId
    issue_sha256: Sha256
    runtime_policy_sha256: Sha256
    budgets_sha256: Sha256
    issue: CodingIssue
    runtime_policy: CodingRuntimePolicy
    budgets: CodingBudgets
    run_manifest: CodingRunManifest
    capabilities: Annotated[
        list[CodingArtifactCapabilityEnvelope], Field(min_length=3, max_length=3)
    ]

    @model_validator(mode="after")
    def authoring_authority_is_coherent(self) -> CodingAuthoringLeaseResponse:
        if (
            self.ticket_id.int == 0
            or self.ticket_deadline.tzinfo is None
            or len(self.run_manifest.tasks) != 1
            or self.run_manifest.coding_run_id != self.coding_run_id
            or self.run_manifest.task_set_manifest_sha256
            != self.task_set_manifest_sha256
            or canonical_digest(self.run_manifest) != self.run_manifest_sha256
            or coding_issue_digest(self.issue) != self.issue_sha256
            or coding_runtime_policy_digest(self.runtime_policy)
            != self.runtime_policy_sha256
            or coding_budgets_digest(self.budgets) != self.budgets_sha256
        ):
            raise ValueError("coding authoring lease material disagrees with authority")
        expected_kinds = [
            CodingArtifactKind.VISIBLE_BUNDLE,
            CodingArtifactKind.MEMORY_BUNDLE,
            CodingArtifactKind.RESOURCE_PROFILE,
        ]
        if [
            capability.artifact_kind for capability in self.capabilities
        ] != expected_kinds:
            raise ValueError(
                "coding authoring capabilities are incomplete or unordered"
            )
        task = self.run_manifest.tasks[0]
        expected_digests = [
            task.visible_bundle_sha256,
            task.memory_bundle_sha256,
            task.resource_profile_sha256,
        ]
        if any(
            capability.ticket_id != self.ticket_id
            or capability.ticket_deadline != self.ticket_deadline
            or capability.delivery_phase is not CodingArtifactDeliveryPhase.AUTHORING
            or capability.sha256 != expected_digest
            for capability, expected_digest in zip(
                self.capabilities, expected_digests, strict=True
            )
        ):
            raise ValueError("coding authoring capabilities disagree with the lease")
        return self


class CodingGradingLeaseRequest(BaseModel):
    """Signed request for one freeze-gated shadow grading lease."""

    model_config = ConfigDict(extra="ignore", strict=True)

    validator_hotkey: Annotated[str, Field(pattern=_SS58_PATTERN)]
    agent_id: UUID
    run_row_id: UUID
    ticket_id: UUID
    freeze_id: UUID
    authoring_evidence_sha256: Sha256
    nonce: UUID
    requested_at: datetime
    signature: Annotated[str, Field(pattern=_SIGNATURE_HEX_PATTERN)]

    @model_validator(mode="after")
    def request_is_coherent(self) -> CodingGradingLeaseRequest:
        if (
            any(
                value.int == 0
                for value in (
                    self.agent_id,
                    self.run_row_id,
                    self.ticket_id,
                    self.freeze_id,
                    self.nonce,
                )
            )
            or self.requested_at.tzinfo is None
        ):
            raise ValueError("coding grading request authority is invalid")
        return self


class CodingGradingLeaseResponse(CodingContractModel):
    schema_name: Literal["dittobench-coding-grading-lease-v1"] = Field(alias="schema")
    coding_contract_version: Literal[1]
    weight_eligible: Literal[False]
    agent_id: UUID
    run_row_id: UUID
    ticket_id: UUID
    ticket_deadline: datetime
    coding_run_id: OpaqueId
    run_manifest_sha256: Sha256
    task_set_manifest_sha256: Sha256
    freeze_id: UUID
    authoring_evidence_sha256: Sha256
    frozen_patch_sha256: Sha256
    frozen_submission_object_key: ContentAddressedKey
    run_manifest: CodingRunManifest
    capabilities: Annotated[
        list[CodingArtifactCapabilityEnvelope], Field(min_length=3, max_length=3)
    ]

    @model_validator(mode="after")
    def grading_authority_is_coherent(self) -> CodingGradingLeaseResponse:
        if (
            any(
                value.int == 0
                for value in (
                    self.agent_id,
                    self.run_row_id,
                    self.ticket_id,
                    self.freeze_id,
                )
            )
            or self.ticket_deadline.tzinfo is None
            or len(self.run_manifest.tasks) != 1
        ):
            raise ValueError("coding grading lease identity is invalid")
        if (
            self.run_manifest.agent_id != str(self.agent_id)
            or self.run_manifest.coding_run_id != self.coding_run_id
            or self.run_manifest.task_set_manifest_sha256
            != self.task_set_manifest_sha256
            or canonical_digest(self.run_manifest) != self.run_manifest_sha256
            or self.frozen_submission_object_key != f"sha256/{self.frozen_patch_sha256}"
        ):
            raise ValueError("coding grading lease material disagrees with authority")
        expected_kinds = [
            CodingArtifactKind.VISIBLE_BUNDLE,
            CodingArtifactKind.RESOURCE_PROFILE,
            CodingArtifactKind.GRADER_BUNDLE,
        ]
        if [item.artifact_kind for item in self.capabilities] != expected_kinds:
            raise ValueError("coding grading capabilities are incomplete or unordered")
        task = self.run_manifest.tasks[0]
        expected_digests = [
            task.visible_bundle_sha256,
            task.resource_profile_sha256,
            task.grader_bundle_sha256,
        ]
        if any(
            capability.ticket_id != self.ticket_id
            or capability.ticket_deadline != self.ticket_deadline
            or capability.delivery_phase is not CodingArtifactDeliveryPhase.GRADING
            or capability.sha256 != digest
            for capability, digest in zip(
                self.capabilities, expected_digests, strict=True
            )
        ):
            raise ValueError("coding grading capabilities disagree with the lease")
        return self


class SubmitCodingShadowResultRequest(BaseModel):
    """Validator-signed terminal shadow result submission."""

    model_config = ConfigDict(extra="ignore", strict=True)

    validator_hotkey: Annotated[str, Field(pattern=_SS58_PATTERN)]
    bench_version: Annotated[int, Field(ge=7)]
    run_row_id: UUID
    ticket_id: UUID
    ticket_deadline: datetime
    agent_artifact_sha256: Sha256
    screened_image_sha256: Sha256
    run_evidence_sha256: Sha256
    evidence: CodingRunEvidence
    signature: Annotated[str, Field(pattern=_SIGNATURE_HEX_PATTERN)]

    @model_validator(mode="after")
    def result_authority_is_coherent(self) -> SubmitCodingShadowResultRequest:
        if (
            self.run_row_id.int == 0
            or self.ticket_id.int == 0
            or self.ticket_deadline.tzinfo is None
            or self.ticket_deadline.utcoffset() is None
            or self.evidence.validator_ticket_id != str(self.ticket_id)
            or coding_run_evidence_transport_digest(self.evidence)
            != self.run_evidence_sha256
        ):
            raise ValueError("coding shadow result authority is invalid")
        return self


class SubmitCodingShadowResultResponse(CodingContractModel):
    agent_id: UUID
    run_row_id: UUID
    ticket_id: UUID
    coding_run_id: OpaqueId
    accepted: Literal[True]
    idempotent: bool
    weight_eligible: Literal[False] = False

    @model_validator(mode="after")
    def response_identity_is_nonzero(self) -> SubmitCodingShadowResultResponse:
        if any(
            value.int == 0 for value in (self.agent_id, self.run_row_id, self.ticket_id)
        ):
            raise ValueError("coding shadow result response identity is invalid")
        return self


class SubmitCodingAuthoringFreezeRequest(BaseModel):
    """One validator-signed immutable transition out of authoring."""

    model_config = ConfigDict(extra="ignore", strict=True)

    validator_hotkey: Annotated[str, Field(pattern=_SS58_PATTERN)]
    agent_id: UUID
    bench_version: Annotated[int, Field(ge=7)]
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
    authoring_transcript_bytes: Annotated[int, Field(ge=0, le=512 << 20)]
    authoring_event_count: Annotated[int, Field(ge=0, le=1_000)]
    frozen_submission_object_key: ContentAddressedKey
    signature: Annotated[str, Field(pattern=_SIGNATURE_HEX_PATTERN)]

    @model_validator(mode="after")
    def freeze_authority_is_coherent(self) -> SubmitCodingAuthoringFreezeRequest:
        if (
            self.agent_id.int == 0
            or self.run_row_id.int == 0
            or self.ticket_id.int == 0
            or self.ticket_deadline.tzinfo is None
            or self.ticket_deadline.utcoffset() is None
        ):
            raise ValueError("coding authoring freeze authority is invalid")
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


class SubmitCodingAuthoringFreezeResponse(CodingContractModel):
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


def coding_authoring_lease_signing_message(
    *,
    validator_hotkey: str,
    ticket_id: UUID,
    nonce: UUID,
    requested_at: datetime,
) -> bytes:
    if requested_at.tzinfo is None or requested_at.utcoffset() is None:
        raise ValueError("coding authoring request timestamp must be timezone-aware")
    timestamp = requested_at.astimezone(UTC).isoformat(timespec="microseconds")
    return "\x00".join(
        (
            "dittobench-coding-authoring-lease:v1",
            validator_hotkey,
            str(ticket_id),
            str(nonce),
            timestamp,
        )
    ).encode()


def coding_grading_lease_signing_message(
    *,
    validator_hotkey: str,
    agent_id: UUID,
    run_row_id: UUID,
    ticket_id: UUID,
    freeze_id: UUID,
    authoring_evidence_sha256: str,
    nonce: UUID,
    requested_at: datetime,
) -> bytes:
    if requested_at.tzinfo is None or requested_at.utcoffset() is None:
        raise ValueError("coding grading request timestamp must be timezone-aware")
    timestamp = requested_at.astimezone(UTC).isoformat(timespec="microseconds")
    return "\x00".join(
        (
            "dittobench-coding-grading-lease:v1",
            validator_hotkey,
            str(agent_id),
            str(run_row_id),
            str(ticket_id),
            str(freeze_id),
            authoring_evidence_sha256,
            str(nonce),
            timestamp,
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
    if ticket_deadline.tzinfo is None or ticket_deadline.utcoffset() is None:
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


def coding_run_evidence_transport_digest(evidence: CodingRunEvidence) -> str:
    """Hash known wire fields; construct evidence with authority-aware helpers."""

    return sha256_hex(_canonical_json_bytes(evidence))


def coding_authoring_evidence_digest(evidence: CodingAuthoringEvidence) -> str:
    return sha256_hex(_canonical_json_bytes(evidence))


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


def _validate_artifact_url(capability: CodingArtifactCapabilityEnvelope) -> None:
    value = capability.url
    if (
        len(value.encode()) > 16 << 10
        or not value.isascii()
        or any(ord(character) < 32 or ord(character) == 127 for character in value)
    ):
        raise ValueError("coding artifact URL is outside bounds")
    try:
        parsed = urlparse(value)
        port = parsed.port
    except ValueError as error:
        raise ValueError("coding artifact URL is invalid") from error
    hostname = parsed.hostname
    if (
        parsed.scheme not in {"http", "https"}
        or not hostname
        or parsed.username is not None
        or parsed.password is not None
        or parsed.fragment
        or not parsed.query
        or ";" in parsed.query
        or not _artifact_percent_encoding(parsed.query)
        or "%" in parsed.path
        or "//" in parsed.path
        or posixpath.normpath(parsed.path) != parsed.path
        or (port is not None and not 1 <= port <= 65_535)
    ):
        raise ValueError("coding artifact URL is invalid")
    if parsed.scheme == "http" and not _artifact_loopback(hostname):
        raise ValueError("coding artifact URL requires HTTPS outside loopback")
    expected = (
        f"/coding-artifacts/v1/{capability.artifact_kind.value}"
        f"/sha256/{capability.sha256}"
    )
    if not parsed.path.endswith(expected):
        raise ValueError("coding artifact URL path disagrees with known fields")
    query: dict[str, list[str]] = {}
    for name, values in parse_qs(parsed.query).items():
        query.setdefault(name.lower(), []).extend(values)
    v4, v2 = query.get("x-amz-signature", []), query.get("signature", [])
    if bool(v4) == bool(v2):
        raise ValueError("coding artifact signature fields are ambiguous")
    if v4:
        dates = query.get("x-amz-date", [])
        durations = query.get("x-amz-expires", [])
        if len(v4) != 1 or len(dates) != 1 or len(durations) != 1:
            raise ValueError("coding artifact v4 signature fields are invalid")
        signed_at = datetime.strptime(dates[0], "%Y%m%dT%H%M%SZ").replace(tzinfo=UTC)
        duration = _artifact_decimal(durations[0])
        if not 60 <= duration <= 900:
            raise ValueError("coding artifact v4 expiry is outside bounds")
        expires_at = signed_at + timedelta(seconds=duration)
    else:
        expires = query.get("expires", [])
        if len(v2) != 1 or len(expires) != 1:
            raise ValueError("coding artifact v2 signature fields are invalid")
        expires_at = datetime.fromtimestamp(_artifact_decimal(expires[0]), tz=UTC)
    if expires_at != capability.expires_at:
        raise ValueError("coding artifact signed expiry disagrees with known fields")


def _artifact_decimal(value: str) -> int:
    if not value or any(character not in "0123456789" for character in value):
        raise ValueError("coding artifact expiry must be ASCII decimal")
    return int(value)


def _artifact_loopback(hostname: str) -> bool:
    if hostname.lower() == "localhost":
        return True
    try:
        return ipaddress.ip_address(hostname).is_loopback
    except ValueError:
        return False


def _artifact_percent_encoding(value: str) -> bool:
    hex_digits = frozenset("0123456789abcdefABCDEF")
    index = 0
    while index < len(value):
        if value[index] != "%":
            index += 1
            continue
        if (
            index + 2 >= len(value)
            or value[index + 1] not in hex_digits
            or value[index + 2] not in hex_digits
        ):
            return False
        index += 3
    return True


def _canonical_json_bytes(value: BaseModel | dict[str, Any] | list[Any]) -> bytes:
    """Serialize one validated known-field projection into deterministic JSON."""

    if isinstance(value, BaseModel):
        reparsed = type(value).model_validate_json(value.model_dump_json(by_alias=True))
        projected: Any = reparsed.model_dump(mode="json", by_alias=True)
    else:
        projected = value
    body = (
        (
            json.dumps(
                projected,
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
    if len(body) > MAX_CANONICAL_JSON_BYTES:
        raise ValueError("canonical coding JSON exceeds 4 MiB")
    return body


def canonical_json_bytes(
    value: CodingRunManifest | CodingSeedRequest | CodingRunRequest,
) -> bytes:
    """Serialize transport models; signed evidence requires authority context."""

    if not isinstance(value, (CodingRunManifest, CodingSeedRequest, CodingRunRequest)):
        raise TypeError(
            "only coding transport models use the generic canonical API; "
            "signed evidence requires a manifest-bound digest API"
        )
    return _canonical_json_bytes(value)


def sha256_hex(body: bytes) -> str:
    return hashlib.sha256(body).hexdigest()


def coding_certification_receipt_digest(
    receipt: CodingCapabilityCertificationReceipt,
) -> str:
    """Hash every known receipt field except the digest itself."""

    projection = receipt.model_dump(mode="json", by_alias=True)
    projection.pop("certification_sha256")
    return sha256_hex(_canonical_json_bytes(projection))


def coding_certification_signing_message(
    *,
    validator_hotkey: str,
    agent_id: UUID,
    bench_version: int,
    ticket_deadline: datetime,
    screened_image_sha256: str,
    certification_sha256: str,
) -> bytes:
    """Bind one receipt to the exact validator lease and screened image."""

    if ticket_deadline.tzinfo is None:
        raise ValueError("coding certification ticket deadline must be timezone-aware")
    lease = ticket_deadline.astimezone(UTC).isoformat(timespec="microseconds")
    fields = (
        "dittobench-coding-certification:v1",
        validator_hotkey,
        str(agent_id),
        str(bench_version),
        lease,
        screened_image_sha256,
        certification_sha256,
    )
    return "\x00".join(fields).encode()


def canonical_digest(
    value: CodingRunManifest | CodingSeedRequest | CodingRunRequest,
) -> str:
    return sha256_hex(canonical_json_bytes(value))


def coding_issue_digest(issue: CodingIssue) -> str:
    """Hash the canonical model-visible issue projection."""

    normalized = CodingIssue.model_validate_json(issue.model_dump_json())
    return sha256_hex(_canonical_json_bytes(normalized))


def coding_runtime_policy_digest(policy: CodingRuntimePolicy) -> str:
    """Hash the canonical model-visible runtime-policy projection."""

    normalized = CodingRuntimePolicy.model_validate_json(policy.model_dump_json())
    return sha256_hex(_canonical_json_bytes(normalized))


def coding_budgets_digest(budgets: CodingBudgets) -> str:
    """Hash the canonical model and workspace budget projection."""

    normalized = CodingBudgets.model_validate_json(budgets.model_dump_json())
    return sha256_hex(_canonical_json_bytes(normalized))


def grader_plan_digest(plan: CodingGraderPlan) -> str:
    """Hash one validated, known-field grader plan projection."""

    normalized = CodingGraderPlan.model_validate_json(
        plan.model_dump_json(by_alias=True)
    )
    return sha256_hex(_canonical_json_bytes(normalized))


def grader_resource_profile_digest(profile: CodingGraderResourceProfile) -> str:
    """Hash one validated grader resource profile projection."""

    normalized = CodingGraderResourceProfile.model_validate_json(
        profile.model_dump_json(by_alias=True)
    )
    return sha256_hex(_canonical_json_bytes(normalized))


def grader_execution_receipt_root(
    plan: CodingGraderPlan,
    receipts: list[CodingGraderExecutionReceipt],
) -> str:
    """Validate plan binding and replay the ordered grader receipt chain."""

    normalized_plan = CodingGraderPlan.model_validate_json(
        plan.model_dump_json(by_alias=True)
    )
    group_plans: dict[str, CodingGraderTestGroupPlan] = {
        group.group: group for group in normalized_plan.test_groups
    }
    expected: list[tuple[str, str | None, CodingGraderCommand, int]] = []
    if normalized_plan.build_required:
        expected.append(("build", None, normalized_plan.build_command, 0))
    expected.extend(
        (
            "test",
            group,
            group_plans[group].command,
            group_plans[group].expected_total,
        )
        for group in normalized_plan.execution_order
    )
    if len(receipts) > len(expected):
        raise ValueError("grader execution receipt chain exceeds its plan")
    previous = _INITIAL_GRADER_RECEIPT_ROOT
    executor_instance_id: str | None = None
    for index, receipt in enumerate(receipts, start=1):
        normalized = CodingGraderExecutionReceipt.model_validate_json(
            receipt.model_dump_json(by_alias=True)
        )
        phase, group, command, total = expected[index - 1]
        command_sha256 = sha256_hex(_canonical_json_bytes(command))
        if executor_instance_id is None:
            executor_instance_id = normalized.executor_instance_id
        if (
            normalized.sequence != index
            or normalized.previous_receipt_sha256 != previous
            or normalized.phase != phase
            or normalized.group != group
            or normalized.command_id != command.id
            or normalized.command_sha256 != command_sha256
            or normalized.executor_instance_id != executor_instance_id
            or normalized.total != total
        ):
            raise ValueError("grader execution receipt chain is not plan-bound")
        previous = sha256_hex(_canonical_json_bytes(normalized))
    return previous


def memory_bundle_digest(
    memories: list[CodingVisibleMemory] | list[dict[str, Any]],
) -> str:
    """Hash only a validated visible-memory projection."""

    normalized = [
        memory
        if isinstance(memory, CodingVisibleMemory)
        else CodingVisibleMemory.model_validate_json(json.dumps(memory))
        for memory in memories
    ]
    projection = {"memories": [memory.model_dump(mode="json") for memory in normalized]}
    return sha256_hex(_canonical_json_bytes(projection))


def parse_canonical_json[ModelT: CodingContractModel](
    model: type[ModelT], body: bytes
) -> ModelT:
    """Parse bounded JSON, rejecting duplicate fields before known-field projection."""

    if not body or len(body) > MAX_CANONICAL_JSON_BYTES:
        raise ValueError("coding JSON size is outside the canonical bound")

    def object_pairs(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
        result: dict[str, Any] = {}
        for key, value in pairs:
            if key in result:
                raise ValueError(f"duplicate JSON field: {key}")
            result[key] = value
        return result

    decoded = json.loads(
        body,
        object_pairs_hook=object_pairs,
        parse_constant=lambda value: (_ for _ in ()).throw(
            ValueError(f"non-finite JSON number: {value}")
        ),
    )
    stack: list[tuple[Any, int]] = [(decoded, 0)]
    while stack:
        value, depth = stack.pop()
        if depth > 32:
            raise ValueError("coding JSON nesting exceeds 32 levels")
        if isinstance(value, dict):
            stack.extend((nested, depth + 1) for nested in value.values())
        elif isinstance(value, list):
            stack.extend((nested, depth + 1) for nested in value)
    return model.model_validate_json(body)


def validate_task_evidence_against_manifest(
    manifest: CodingRunManifest,
    validator_ticket_id: str,
    evidence: CodingTaskEvidence,
) -> None:
    """Bind task evidence to one exact task in its canonical run manifest."""

    manifest = type(manifest).model_validate_json(
        manifest.model_dump_json(by_alias=True)
    )
    evidence = type(evidence).model_validate_json(
        evidence.model_dump_json(by_alias=True)
    )
    _validate_opaque_id(validator_ticket_id)
    matching = [
        task
        for task in manifest.tasks
        if (task.case_id, task.variant_id)
        == (evidence.task.case_id, evidence.task.variant_id)
    ]
    if len(matching) != 1 or matching[0] != evidence.task:
        raise ValueError("task evidence does not match exactly one manifest task")
    if (
        evidence.coding_run_id != manifest.coding_run_id
        or evidence.validator_ticket_id != validator_ticket_id
        or evidence.agent_id != manifest.agent_id
        or evidence.agent_artifact_sha256 != manifest.agent_artifact_sha256
        or evidence.corpus_release_id != manifest.corpus_release_id
        or evidence.task_set_id != manifest.task_set_id
        or evidence.task_set_manifest_sha256 != manifest.task_set_manifest_sha256
    ):
        raise ValueError("task evidence identity does not match run manifest")
    if (
        evidence.authoring is not None
        and evidence.authoring.model.inference_grant_sha256
        != manifest.inference_grant_sha256
    ):
        raise ValueError("task evidence inference grant does not match manifest")
    if evidence.grader is not None:
        selected = matching[0]
        if (
            evidence.grader.grader_contract_sha256 != manifest.grader_contract_sha256
            or evidence.grader.grader_bundle_sha256 != selected.grader_bundle_sha256
            or evidence.grader.grader_image_digest != selected.grader_image_digest
            or evidence.grader.grader_platform != selected.grader_platform
            or evidence.grader.test_manifest_sha256 != selected.test_manifest_sha256
            or evidence.grader.grader_plan_sha256 != selected.grader_plan_sha256
            or evidence.grader.resource_profile_sha256
            != selected.resource_profile_sha256
        ):
            raise ValueError("task evidence grader authority does not match manifest")


def task_evidence_digest(
    manifest: CodingRunManifest,
    validator_ticket_id: str,
    evidence: CodingTaskEvidence,
) -> str:
    """Hash task evidence only after binding it to lease authority."""

    validate_task_evidence_against_manifest(manifest, validator_ticket_id, evidence)
    return sha256_hex(_canonical_json_bytes(evidence))


def validate_run_evidence_against_manifest(
    manifest: CodingRunManifest,
    validator_ticket_id: str,
    evidence: CodingRunEvidence,
    task_evidence: list[CodingTaskEvidence],
) -> None:
    """Replay run aggregation against the manifest and per-task evidence roots."""

    manifest = type(manifest).model_validate_json(
        manifest.model_dump_json(by_alias=True)
    )
    evidence = type(evidence).model_validate_json(
        evidence.model_dump_json(by_alias=True)
    )
    task_evidence = [
        type(item).model_validate_json(item.model_dump_json(by_alias=True))
        for item in task_evidence
    ]
    _validate_opaque_id(validator_ticket_id)
    if (
        evidence.coding_run_id != manifest.coding_run_id
        or evidence.validator_ticket_id != validator_ticket_id
    ):
        raise ValueError("run evidence identity does not match lease authority")
    if evidence.run_manifest_sha256 != canonical_digest(manifest):
        raise ValueError("run evidence does not bind the canonical run manifest")
    if evidence.task_set_manifest_sha256 != manifest.task_set_manifest_sha256:
        raise ValueError("run evidence task-set digest does not match manifest")
    if len(evidence.tasks) != len(manifest.tasks) or len(task_evidence) != len(
        manifest.tasks
    ):
        raise ValueError("run evidence cardinality does not match manifest")

    by_identity: dict[tuple[str, str], CodingTaskEvidence] = {}
    for item in task_evidence:
        validate_task_evidence_against_manifest(manifest, validator_ticket_id, item)
        identity = (item.task.case_id, item.task.variant_id)
        if identity in by_identity:
            raise ValueError("duplicate per-task evidence identity")
        by_identity[identity] = item

    for result, selected in zip(evidence.tasks, manifest.tasks, strict=True):
        identity = (result.case_id, result.variant_id)
        if identity != (selected.case_id, selected.variant_id):
            raise ValueError("run evidence task order does not match manifest")
        matched = by_identity.get(identity)
        if (
            matched is None
            or result.task_evidence_sha256
            != task_evidence_digest(manifest, validator_ticket_id, matched)
            or result.terminal_domain != matched.terminal_domain
            or result.repair_score_micros != matched.repair_score_micros
        ):
            raise ValueError("run task result does not match per-task evidence")


def run_evidence_digest(
    manifest: CodingRunManifest,
    validator_ticket_id: str,
    evidence: CodingRunEvidence,
    task_evidence: list[CodingTaskEvidence],
) -> str:
    """Hash run evidence only after replaying manifest and task authority."""

    validate_run_evidence_against_manifest(
        manifest, validator_ticket_id, evidence, task_evidence
    )
    return sha256_hex(_canonical_json_bytes(evidence))


__all__ = [
    "CODING_CONTRACT_VERSION",
    "REPAIR_SCORE_RESOLVED_MICROS",
    "CodingArtifactAudience",
    "CodingArtifactCapabilityEnvelope",
    "CodingArtifactDeliveryPhase",
    "CodingArtifactKind",
    "CodingAuthoringLeaseRequest",
    "CodingAuthoringLeaseResponse",
    "CodingAuthoringEvidence",
    "CodingGradingLeaseRequest",
    "CodingGradingLeaseResponse",
    "CodingBudgets",
    "CodingBuildEvidence",
    "CodingCapabilityCertificationReceipt",
    "CodingCertificationStage",
    "CodingCertificationStatus",
    "CodingContractModel",
    "CodingGraderEvidence",
    "CodingGraderCommand",
    "CodingGraderExecutionReceipt",
    "CodingGraderLimits",
    "CodingGraderPlan",
    "CodingGraderResourceProfile",
    "CodingGraderTestGroupPlan",
    "CodingIssue",
    "CodingManifestTask",
    "CodingModelEvidence",
    "CodingModelUsageStatus",
    "CodingRunEvidence",
    "CodingRunManifest",
    "CodingRunRequest",
    "CodingRuntimePolicy",
    "CodingSeedRequest",
    "CodingTaskEvidence",
    "CodingTaskResult",
    "CodingTerminalDomain",
    "CodingTestGroupEvidence",
    "CodingVisibleMemory",
    "SubmitCodingCertificationRequest",
    "SubmitCodingCertificationResponse",
    "SubmitCodingAuthoringFreezeRequest",
    "SubmitCodingAuthoringFreezeResponse",
    "SubmitCodingShadowResultRequest",
    "SubmitCodingShadowResultResponse",
    "canonical_digest",
    "canonical_json_bytes",
    "coding_certification_receipt_digest",
    "coding_certification_signing_message",
    "coding_authoring_lease_signing_message",
    "coding_grading_lease_signing_message",
    "coding_shadow_result_signing_message",
    "coding_run_evidence_transport_digest",
    "coding_authoring_evidence_digest",
    "coding_authoring_freeze_signing_message",
    "coding_budgets_digest",
    "coding_issue_digest",
    "coding_runtime_policy_digest",
    "grader_execution_receipt_root",
    "grader_plan_digest",
    "grader_resource_profile_digest",
    "memory_bundle_digest",
    "parse_canonical_json",
    "run_evidence_digest",
    "task_evidence_digest",
    "validate_run_evidence_against_manifest",
    "validate_task_evidence_against_manifest",
]
