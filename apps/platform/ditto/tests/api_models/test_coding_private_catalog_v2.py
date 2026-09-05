from __future__ import annotations

import json
from pathlib import Path

import pytest
from pydantic import ValidationError

from ditto.api_models.coding_private_catalog_v2 import (
    CodingMemoryConditionV2,
    CodingPrivateCatalogV2Task,
    coding_private_catalog_v2_task_digest,
)


def _task() -> CodingPrivateCatalogV2Task:
    draft = CodingPrivateCatalogV2Task.model_construct(
        schema_name="dittobench-coding-private-catalog-task-v2",
        coding_contract_version=2,
        weight_eligible=False,
        corpus_release_id="coding-private-v2-r1",
        catalog_index=7,
        task_version_id="private-group-001-v1",
        base_task_group_id="private-group-001",
        condition=CodingMemoryConditionV2.RELEVANT,
        repository_epoch="epoch-stream-2",
        private_release_sha256="1" * 64,
        group_manifest_sha256="2" * 64,
        visible_snapshot_tree_sha256="3" * 64,
        visible_issue_sha256="b" * 64,
        hidden_grader_tree_sha256="4" * 64,
        memory_bundle_sha256="5" * 64,
        runtime_policy_sha256="6" * 64,
        resource_profile_sha256="7" * 64,
        calibration_sha256="8" * 64,
        semantic_review_sha256="9" * 64,
        runner_profile_sha256="a" * 64,
        task_commitment_sha256="0" * 64,
    )
    return CodingPrivateCatalogV2Task.model_validate(
        {
            **draft.model_dump(mode="json", by_alias=True),
            "task_commitment_sha256": coding_private_catalog_v2_task_digest(draft),
        }
    )


def test_private_catalog_v2_task_binds_one_condition() -> None:
    task = _task()
    assert task.weight_eligible is False
    assert task.condition == CodingMemoryConditionV2.RELEVANT
    assert task.task_commitment_sha256 == coding_private_catalog_v2_task_digest(task)


def test_private_catalog_v2_task_rejects_commitment_or_weight_drift() -> None:
    raw = _task().model_dump(mode="json", by_alias=True)
    raw["weight_eligible"] = True
    with pytest.raises(ValidationError):
        CodingPrivateCatalogV2Task.model_validate(raw)
    raw = _task().model_dump(mode="json", by_alias=True)
    raw["future_field"] = "ignored"
    parsed = CodingPrivateCatalogV2Task.model_validate(raw)
    assert parsed == _task()
    assert "future_field" not in parsed.model_dump(mode="json", by_alias=True)
    assert (
        coding_private_catalog_v2_task_digest(parsed) == parsed.task_commitment_sha256
    )
    raw = _task().model_dump(mode="json", by_alias=True)
    del raw["visible_issue_sha256"]
    with pytest.raises(ValidationError):
        CodingPrivateCatalogV2Task.model_validate(raw)


def test_private_catalog_v2_vector_is_canonical() -> None:
    vector = (
        Path(__file__).resolve().parents[5]
        / "packages"
        / "dittobench-coding-contract"
        / "testdata"
        / "coding_private_catalog_v2.json"
    )
    task = CodingPrivateCatalogV2Task.model_validate(json.loads(vector.read_bytes()))
    assert coding_private_catalog_v2_task_digest(task) == task.task_commitment_sha256
    raw = _task().model_dump(mode="json", by_alias=True)
    raw["condition"] = "unknown"
    with pytest.raises(ValidationError):
        CodingPrivateCatalogV2Task.model_validate(raw)
