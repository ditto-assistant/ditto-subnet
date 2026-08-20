"""Shadow-only DittoBench Coding contract v1 models.

These models define the known-field projection used by the future scorer and
validator.  They do not activate a benchmark version or make coding evidence
weight eligible.
"""

from __future__ import annotations

import hashlib
import json
import unicodedata
from enum import StrEnum
from typing import Annotated, Any, Literal

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


def canonical_digest(
    value: CodingRunManifest | CodingSeedRequest | CodingRunRequest,
) -> str:
    return sha256_hex(canonical_json_bytes(value))


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
    "CodingAuthoringEvidence",
    "CodingBudgets",
    "CodingBuildEvidence",
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
    "canonical_digest",
    "canonical_json_bytes",
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
