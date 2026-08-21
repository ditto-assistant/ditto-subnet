"""Shadow private-catalog selection contracts for DittoBench Coding."""

from __future__ import annotations

import unicodedata
from datetime import UTC, datetime
from typing import Annotated, Literal
from uuid import UUID

from pydantic import AfterValidator, Field, field_validator, model_validator

from ditto.api_models.coding_canonical import coding_canonical_sha256
from ditto.api_models.coding_catalog import OciDigest
from ditto.api_models.coding_evaluation import (
    BlockHash,
    CodingEvaluationModel,
    OpaqueId,
    Sha256,
    ShortName,
)

_MAX_CANONICAL_JSON_BYTES = 4 << 20


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


def _validate_command_id(value: str) -> str:
    if len(value.encode()) > 80 or any(
        character.isspace() or unicodedata.category(character) == "Cc"
        for character in value
    ):
        raise ValueError("command ID is outside coding contract bounds")
    return value


SafeRelativePath = Annotated[
    str,
    Field(min_length=1, max_length=256),
    AfterValidator(_validate_relative_path),
]
CommandId = Annotated[
    str,
    Field(min_length=1, max_length=80),
    AfterValidator(_validate_command_id),
]


class CodingCatalogIssue(CodingEvaluationModel):
    title: Annotated[str, Field(max_length=1024)]
    description: Annotated[str, Field(min_length=1, max_length=64 * 1024)]
    constraints: Annotated[list[str], Field(max_length=64)]

    @field_validator("constraints")
    @classmethod
    def constraints_fit_byte_bounds(cls, values: list[str]) -> list[str]:
        if any(not value or len(value.encode()) > 4096 for value in values):
            raise ValueError("constraints must contain 1..=4096 UTF-8 bytes")
        return values

    @model_validator(mode="after")
    def issue_fits_byte_bounds(self) -> CodingCatalogIssue:
        if (
            len(self.title.encode()) > 1024
            or len(self.description.encode()) > 64 * 1024
        ):
            raise ValueError("issue title or description exceeds its UTF-8 byte bound")
        return self


class CodingCatalogRuntimePolicy(CodingEvaluationModel):
    editable_paths: Annotated[list[SafeRelativePath], Field(max_length=64)]
    test_command_ids: Annotated[list[CommandId], Field(max_length=64)]
    build_command_ids: Annotated[list[CommandId], Field(max_length=64)]

    @model_validator(mode="after")
    def collections_are_unique(self) -> CodingCatalogRuntimePolicy:
        for label, values in (
            ("editable_paths", self.editable_paths),
            ("test_command_ids", self.test_command_ids),
            ("build_command_ids", self.build_command_ids),
        ):
            if len(set(values)) != len(values):
                raise ValueError(f"{label} must not contain duplicates")
        return self


class CodingCatalogBudgets(CodingEvaluationModel):
    model_input_tokens: Annotated[int, Field(ge=1, le=2_000_000)]
    model_output_tokens: Annotated[int, Field(ge=1, le=250_000)]
    workspace_tool_calls: Annotated[int, Field(ge=1, le=1_000)]
    wall_time_seconds: Annotated[int, Field(ge=1, le=7_200)]


class CodingCatalogManifestTask(CodingEvaluationModel):
    """The miner-visible task identity committed by one private catalog leaf."""

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


class CodingCatalogTaskPayload(CodingEvaluationModel):
    """Known fields whose digest is one immutable private task version."""

    schema_name: Literal["dittobench-coding-catalog-task-v1"] = Field(alias="schema")
    coding_contract_version: Literal[1]
    weight_eligible: Literal[False]
    corpus_release_id: OpaqueId
    catalog_index: Annotated[int, Field(ge=0, le=999_999)]
    task_version_id: OpaqueId
    repository_epoch: OpaqueId
    issue_sha256: Sha256
    runtime_policy_sha256: Sha256
    budgets_sha256: Sha256
    task: CodingCatalogManifestTask


class CodingCatalogTaskVersion(CodingEvaluationModel):
    payload: CodingCatalogTaskPayload
    task_commitment_sha256: Sha256

    @model_validator(mode="after")
    def task_commitment_matches_payload(self) -> CodingCatalogTaskVersion:
        if (
            coding_catalog_task_commitment_digest(self.payload)
            != self.task_commitment_sha256
        ):
            raise ValueError("task_commitment_sha256 does not match task payload")
        return self


class CodingCatalogMembershipProof(CodingEvaluationModel):
    """Position-bound proof for one private catalog task commitment."""

    schema_name: Literal["dittobench-coding-catalog-membership-proof-v1"] = Field(
        alias="schema"
    )
    coding_contract_version: Literal[1]
    corpus_release_id: OpaqueId
    catalog_merkle_root: Sha256
    task_version_count: Annotated[int, Field(ge=1, le=1_000_000)]
    catalog_index: Annotated[int, Field(ge=0, le=999_999)]
    task_commitment_sha256: Sha256
    sibling_sha256: Annotated[list[Sha256], Field(max_length=20)]
    catalog_membership_proof_sha256: Sha256

    @model_validator(mode="after")
    def proof_shape_and_digest_are_canonical(self) -> CodingCatalogMembershipProof:
        expected_depth = (self.task_version_count - 1).bit_length()
        if (
            self.catalog_index >= self.task_version_count
            or len(self.sibling_sha256) != expected_depth
        ):
            raise ValueError("catalog membership proof has a noncanonical shape")
        if (
            coding_catalog_membership_proof_digest(self)
            != self.catalog_membership_proof_sha256
        ):
            raise ValueError(
                "catalog_membership_proof_sha256 does not match known fields"
            )
        return self


class CodingSelectionAssignmentFields(CodingEvaluationModel):
    schema_name: Literal["dittobench-coding-selection-assignment-v1"] = Field(
        alias="schema"
    )
    coding_contract_version: Literal[1]
    weight_eligible: Literal[False]
    bench_version: Annotated[int, Field(ge=7)]
    coding_run_id: OpaqueId
    agent_id: UUID
    agent_artifact_sha256: Sha256
    screened_image_sha256: Sha256
    corpus_release_id: OpaqueId
    catalog_commitment_sha256: Sha256
    anchor_block_number: Annotated[int, Field(ge=1, le=(1 << 63) - 1)]
    anchor_block_hash: BlockHash
    selection_delay_blocks: Annotated[int, Field(ge=1, le=10_000)]
    selection_block_number: Annotated[int, Field(ge=1, le=(1 << 63) - 1)]
    assigned_at: datetime
    task_count: Literal[1]

    @field_validator("assigned_at")
    @classmethod
    def assigned_at_is_aware(cls, value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("selection assignment time must be timezone-aware")
        return value.astimezone(UTC)

    @model_validator(mode="after")
    def selected_height_follows_anchor(self) -> CodingSelectionAssignmentFields:
        if (
            self.anchor_block_number + self.selection_delay_blocks
            != self.selection_block_number
        ):
            raise ValueError(
                "selection block number must equal anchor plus fixed delay"
            )
        return self


class CodingSelectionAssignment(CodingSelectionAssignmentFields):
    """Immutable height assignment consumed by the selector core."""

    assignment_sha256: Sha256

    @model_validator(mode="after")
    def assignment_digest_matches(self) -> CodingSelectionAssignment:
        if coding_selection_assignment_digest(self) != self.assignment_sha256:
            raise ValueError("assignment_sha256 does not match known fields")
        return self


class CodingSelectionProof(CodingEvaluationModel):
    """Reproducible proof of the selected affine-permutation position."""

    schema_name: Literal["dittobench-coding-selection-proof-v1"] = Field(alias="schema")
    coding_contract_version: Literal[1]
    assignment_sha256: Sha256
    selection_block_hash: BlockHash
    candidate_probe: Annotated[int, Field(ge=0, le=999_999)]
    catalog_index: Annotated[int, Field(ge=0, le=999_999)]
    task_version_id: OpaqueId
    task_commitment_sha256: Sha256
    catalog_membership_proof_sha256: Sha256
    selection_proof_sha256: Sha256

    @model_validator(mode="after")
    def proof_digest_matches(self) -> CodingSelectionProof:
        if coding_selection_proof_digest(self) != self.selection_proof_sha256:
            raise ValueError("selection_proof_sha256 does not match known fields")
        return self


class CodingSelectedTask(CodingEvaluationModel):
    manifest_index: Annotated[int, Field(ge=0, le=99)]
    task_version_id: OpaqueId
    task_commitment_sha256: Sha256
    selection_proof_sha256: Sha256
    catalog_membership_proof_sha256: Sha256
    repository_epoch: OpaqueId
    issue_sha256: Sha256
    runtime_policy_sha256: Sha256
    budgets_sha256: Sha256
    task: CodingCatalogManifestTask


class CodingTaskSetManifest(CodingEvaluationModel):
    """Private selected-task identity; its digest enters the public run manifest."""

    schema_name: Literal["dittobench-coding-task-set-v1"] = Field(alias="schema")
    coding_contract_version: Literal[1]
    weight_eligible: Literal[False]
    coding_run_id: OpaqueId
    assignment_sha256: Sha256
    selection_block_number: Annotated[int, Field(ge=1, le=(1 << 63) - 1)]
    selection_block_hash: BlockHash
    tasks: Annotated[list[CodingSelectedTask], Field(min_length=1, max_length=100)]

    @model_validator(mode="after")
    def selected_tasks_are_unique_and_sorted(self) -> CodingTaskSetManifest:
        indexes = [task.manifest_index for task in self.tasks]
        identities = [task.task_version_id for task in self.tasks]
        if indexes != list(range(len(self.tasks))) or len(set(identities)) != len(
            identities
        ):
            raise ValueError("selected tasks must be unique and manifest ordered")
        return self


class CodingSelectionRunManifest(CodingEvaluationModel):
    """Platform mirror of the shared coding run-manifest v1 contract."""

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
    selection_block_number: Annotated[int, Field(ge=1, le=(1 << 64) - 1)]
    selection_block_hash: BlockHash
    inference_grant_sha256: Sha256
    grader_contract_sha256: Sha256
    task_set_id: OpaqueId
    task_set_manifest_sha256: Sha256
    tasks: Annotated[
        list[CodingCatalogManifestTask], Field(min_length=1, max_length=100)
    ]

    @model_validator(mode="after")
    def tasks_are_unique_and_sorted(self) -> CodingSelectionRunManifest:
        identities = [(task.case_id, task.variant_id) for task in self.tasks]
        if identities != sorted(identities) or len(set(identities)) != len(identities):
            raise ValueError(
                "manifest tasks must be unique and sorted by case_id, variant_id"
            )
        return self


def coding_catalog_task_commitment_digest(
    payload: CodingCatalogTaskPayload,
) -> str:
    payload = CodingCatalogTaskPayload.model_validate_json(
        payload.model_dump_json(by_alias=True)
    )
    return coding_canonical_sha256(
        payload.model_dump(mode="json", by_alias=True),
        maximum_bytes=_MAX_CANONICAL_JSON_BYTES,
        label="coding catalog task",
    )


def coding_catalog_issue_digest(issue: CodingCatalogIssue) -> str:
    issue = CodingCatalogIssue.model_validate_json(issue.model_dump_json())
    return coding_canonical_sha256(
        issue.model_dump(mode="json"),
        maximum_bytes=_MAX_CANONICAL_JSON_BYTES,
        label="coding catalog issue",
    )


def coding_catalog_runtime_policy_digest(
    policy: CodingCatalogRuntimePolicy,
) -> str:
    policy = CodingCatalogRuntimePolicy.model_validate_json(policy.model_dump_json())
    return coding_canonical_sha256(
        policy.model_dump(mode="json"),
        maximum_bytes=_MAX_CANONICAL_JSON_BYTES,
        label="coding catalog runtime policy",
    )


def coding_catalog_budgets_digest(budgets: CodingCatalogBudgets) -> str:
    budgets = CodingCatalogBudgets.model_validate_json(budgets.model_dump_json())
    return coding_canonical_sha256(
        budgets.model_dump(mode="json"),
        maximum_bytes=_MAX_CANONICAL_JSON_BYTES,
        label="coding catalog budgets",
    )


def coding_catalog_membership_proof_digest(
    proof: CodingCatalogMembershipProof,
) -> str:
    projection = proof.model_dump(mode="json", by_alias=True)
    projection.pop("catalog_membership_proof_sha256")
    return coding_canonical_sha256(
        projection,
        maximum_bytes=_MAX_CANONICAL_JSON_BYTES,
        label="coding catalog membership proof",
    )


def coding_selection_assignment_digest(assignment: CodingSelectionAssignment) -> str:
    projection = assignment.model_dump(mode="json", by_alias=True)
    projection.pop("assignment_sha256")
    return coding_canonical_sha256(
        projection,
        maximum_bytes=_MAX_CANONICAL_JSON_BYTES,
        label="coding selection assignment",
    )


def bind_coding_selection_assignment(
    values: dict[str, object],
) -> CodingSelectionAssignment:
    """Validate assignment fields before binding their canonical digest."""

    if "assignment_sha256" in values:
        raise ValueError("assignment_sha256 must not be caller supplied")
    fields = CodingSelectionAssignmentFields.model_validate(values)
    projection = fields.model_dump(mode="json", by_alias=True)
    projection["assignment_sha256"] = coding_canonical_sha256(
        projection,
        maximum_bytes=_MAX_CANONICAL_JSON_BYTES,
        label="coding selection assignment",
    )
    return CodingSelectionAssignment.model_validate(projection)


def coding_selection_proof_digest(proof: CodingSelectionProof) -> str:
    projection = proof.model_dump(mode="json", by_alias=True)
    projection.pop("selection_proof_sha256")
    return coding_canonical_sha256(
        projection,
        maximum_bytes=_MAX_CANONICAL_JSON_BYTES,
        label="coding selection proof",
    )


def coding_task_set_manifest_digest(manifest: CodingTaskSetManifest) -> str:
    manifest = CodingTaskSetManifest.model_validate_json(
        manifest.model_dump_json(by_alias=True)
    )
    return coding_canonical_sha256(
        manifest.model_dump(mode="json", by_alias=True),
        maximum_bytes=_MAX_CANONICAL_JSON_BYTES,
        label="coding task-set manifest",
    )


def coding_selection_run_manifest_digest(
    manifest: CodingSelectionRunManifest,
) -> str:
    manifest = CodingSelectionRunManifest.model_validate_json(
        manifest.model_dump_json(by_alias=True)
    )
    return coding_canonical_sha256(
        manifest.model_dump(mode="json", by_alias=True),
        maximum_bytes=_MAX_CANONICAL_JSON_BYTES,
        label="coding run manifest",
    )
