"""Shadow-only v2 leaves that bind a private group to one memory condition."""

from __future__ import annotations

from enum import StrEnum
from typing import Annotated, Literal

from pydantic import ConfigDict, Field, model_validator

from ditto.api_models.coding_canonical import coding_canonical_sha256
from ditto.api_models.coding_evaluation import CodingEvaluationModel, OpaqueId, Sha256


class CodingMemoryConditionV2(StrEnum):
    NONE = "v0_none"
    RELEVANT = "v1_relevant"
    IRRELEVANT = "v2_irrelevant"
    STALE_CONFLICT = "v3_stale_conflict"
    CURRENT_OVERRIDE = "v4_current_override"


class CodingPrivateCatalogV2Task(CodingEvaluationModel):
    """One sealed v2 catalog leaf; condition/group linkage stays private."""

    model_config = ConfigDict(
        extra="ignore",
        frozen=True,
        serialize_by_alias=True,
        validate_by_name=True,
    )
    schema_name: Literal["dittobench-coding-private-catalog-task-v2"] = Field(
        alias="schema"
    )
    coding_contract_version: Literal[2]
    weight_eligible: Literal[False]
    corpus_release_id: OpaqueId
    catalog_index: Annotated[int, Field(ge=0, le=999_999)]
    task_version_id: OpaqueId
    base_task_group_id: OpaqueId
    condition: CodingMemoryConditionV2
    repository_epoch: OpaqueId
    private_release_sha256: Sha256
    group_manifest_sha256: Sha256
    visible_snapshot_tree_sha256: Sha256
    visible_issue_sha256: Sha256
    hidden_grader_tree_sha256: Sha256
    memory_bundle_sha256: Sha256
    runtime_policy_sha256: Sha256
    resource_profile_sha256: Sha256
    calibration_sha256: Sha256
    semantic_review_sha256: Sha256
    runner_profile_sha256: Sha256
    task_commitment_sha256: Sha256

    @model_validator(mode="after")
    def commitment_matches_known_fields(self) -> CodingPrivateCatalogV2Task:
        if coding_private_catalog_v2_task_digest(self) != self.task_commitment_sha256:
            raise ValueError("task_commitment_sha256 does not match known fields")
        return self


def coding_private_catalog_v2_task_digest(task: CodingPrivateCatalogV2Task) -> str:
    projection = task.model_dump(mode="json", by_alias=True)
    projection.pop("task_commitment_sha256", None)
    return coding_canonical_sha256(
        projection,
        maximum_bytes=64 << 10,
        label="private v2 catalog task",
    )


__all__ = [
    "CodingMemoryConditionV2",
    "CodingPrivateCatalogV2Task",
    "coding_private_catalog_v2_task_digest",
]
