"""Tests for typed shadow coding terminal failure classification."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from ditto.api_models.coding import (
    CodingAuthoringEvidence,
    CodingGraderEvidence,
    CodingRunManifest,
    CodingTaskEvidence,
    CodingTerminalDomain,
)
from ditto.validator.coding_failure import (
    CodingFailureClassificationError,
    CodingFailureCode,
    CodingFailureStage,
    build_coding_failure_task_evidence,
)
from ditto.validator.coding_terminal import build_coding_run_evidence

_CODES = {
    CodingFailureStage.POST_LEASE_TRANSPORT: CodingFailureCode.POST_LEASE_TRANSPORT,
    CodingFailureStage.TASK_MATERIAL: CodingFailureCode.TASK_MATERIAL_INVALID,
    CodingFailureStage.AUTHORING_INFRASTRUCTURE: CodingFailureCode.AUTHORING_RUNTIME,
    CodingFailureStage.CANDIDATE_INTEGRITY: (
        CodingFailureCode.CANDIDATE_POLICY_VIOLATION
    ),
    CodingFailureStage.GRADING_INFRASTRUCTURE: CodingFailureCode.GRADING_RUNTIME,
    CodingFailureStage.REPAIR_FAILURE: CodingFailureCode.GRADER_TESTS_FAILED,
    CodingFailureStage.CONTROL_PLANE_INTEGRITY: (
        CodingFailureCode.CONTROL_PLANE_MISMATCH
    ),
}

_TESTDATA = (
    Path(__file__).parents[3] / "packages" / "dittobench-coding-contract" / "testdata"
)


def _vector(name: str) -> dict[str, Any]:
    return json.loads((_TESTDATA / name).read_text(encoding="utf-8"))


def _manifest() -> CodingRunManifest:
    return CodingRunManifest.model_validate_json(
        json.dumps(_vector("coding_selection_v1.json")["run_manifest"])
    )


def _components() -> tuple[CodingAuthoringEvidence, CodingGraderEvidence]:
    contract = _vector("coding_contract_v1.json")
    task = CodingTaskEvidence.model_validate_json(json.dumps(contract["task_evidence"]))
    assert task.authoring is not None
    assert task.grader is not None
    manifest = _manifest()
    selected = manifest.tasks[0]
    model = task.authoring.model.model_copy(
        update={"inference_grant_sha256": manifest.inference_grant_sha256}
    )
    authoring = task.authoring.model_copy(update={"model": model})
    groups = list(task.grader.test_groups)
    groups[0] = groups[0].model_copy(update={"passed": groups[0].total - 1})
    grader = task.grader.model_copy(
        update={
            "grader_contract_sha256": manifest.grader_contract_sha256,
            "grader_bundle_sha256": selected.grader_bundle_sha256,
            "grader_image_digest": selected.grader_image_digest,
            "grader_platform": selected.grader_platform,
            "test_manifest_sha256": selected.test_manifest_sha256,
            "grader_plan_sha256": selected.grader_plan_sha256,
            "resource_profile_sha256": selected.resource_profile_sha256,
            "test_groups": groups,
        }
    )
    assert not grader.resolved()
    return authoring, grader


def _build(
    stage: CodingFailureStage,
    *,
    authoring: CodingAuthoringEvidence | None = None,
    grader: CodingGraderEvidence | None = None,
    failure_code: CodingFailureCode | None = None,
) -> CodingTaskEvidence:
    selected = _manifest().tasks[0]
    return build_coding_failure_task_evidence(
        _manifest(),
        validator_ticket_id="33333333-3333-4333-8333-333333333333",
        case_id=selected.case_id,
        variant_id=selected.variant_id,
        stage=stage,
        failure_code=failure_code or _CODES[stage],
        authoring=authoring,
        grader=grader,
    )


@pytest.mark.parametrize(
    ("stage", "terminal_domain", "component_mode"),
    [
        (
            CodingFailureStage.POST_LEASE_TRANSPORT,
            CodingTerminalDomain.VALIDATOR_INFRASTRUCTURE,
            "none",
        ),
        (
            CodingFailureStage.TASK_MATERIAL,
            CodingTerminalDomain.TASK_INVALID,
            "none",
        ),
        (
            CodingFailureStage.AUTHORING_INFRASTRUCTURE,
            CodingTerminalDomain.VALIDATOR_INFRASTRUCTURE,
            "none",
        ),
        (
            CodingFailureStage.CANDIDATE_INTEGRITY,
            CodingTerminalDomain.CANDIDATE_INTEGRITY,
            "authoring_and_failed_grader",
        ),
        (
            CodingFailureStage.GRADING_INFRASTRUCTURE,
            CodingTerminalDomain.VALIDATOR_INFRASTRUCTURE,
            "authoring",
        ),
        (
            CodingFailureStage.REPAIR_FAILURE,
            CodingTerminalDomain.REPAIR_FAILURE,
            "authoring_and_failed_grader",
        ),
        (
            CodingFailureStage.CONTROL_PLANE_INTEGRITY,
            CodingTerminalDomain.CONTROL_PLANE_INTEGRITY,
            "authoring",
        ),
    ],
)
def test_typed_stage_maps_to_one_terminal_domain(
    stage: CodingFailureStage,
    terminal_domain: CodingTerminalDomain,
    component_mode: str,
) -> None:
    authoring, grader = _components()
    evidence = _build(
        stage,
        authoring=(authoring if "authoring" in component_mode else None),
        grader=(grader if "grader" in component_mode else None),
    )
    assert evidence.terminal_domain is terminal_domain
    assert evidence.failure_code == _CODES[stage].value
    assert evidence.repair_score_micros == 0


@pytest.mark.parametrize(
    ("stage", "count_field", "scoreable"),
    [
        (CodingFailureStage.TASK_MATERIAL, "invalid_count", 0),
        (CodingFailureStage.CANDIDATE_INTEGRITY, "candidate_integrity_count", 1),
        (CodingFailureStage.REPAIR_FAILURE, "repair_failure_count", 1),
    ],
)
def test_classified_task_feeds_validator_owned_aggregation(
    stage: CodingFailureStage,
    count_field: str,
    scoreable: int,
) -> None:
    authoring, grader = _components()
    task = _build(
        stage,
        authoring=(
            authoring
            if stage
            in {
                CodingFailureStage.CANDIDATE_INTEGRITY,
                CodingFailureStage.REPAIR_FAILURE,
            }
            else None
        ),
        grader=(grader if stage is CodingFailureStage.REPAIR_FAILURE else None),
    )
    run = build_coding_run_evidence(
        _manifest(),
        task.validator_ticket_id,
        [task],
    )
    assert getattr(run, count_field) == 1
    assert run.scoreable_task_count == scoreable
    assert run.repair_mean_micros == 0


@pytest.mark.parametrize(
    ("stage", "component_mode"),
    [
        (CodingFailureStage.POST_LEASE_TRANSPORT, "authoring"),
        (CodingFailureStage.TASK_MATERIAL, "authoring"),
        (CodingFailureStage.AUTHORING_INFRASTRUCTURE, "authoring"),
        (CodingFailureStage.CANDIDATE_INTEGRITY, "none"),
        (CodingFailureStage.GRADING_INFRASTRUCTURE, "none"),
        (CodingFailureStage.REPAIR_FAILURE, "authoring"),
        (CodingFailureStage.CONTROL_PLANE_INTEGRITY, "grader"),
    ],
)
def test_stage_rejects_missing_or_forbidden_components(
    stage: CodingFailureStage,
    component_mode: str,
) -> None:
    authoring, grader = _components()
    with pytest.raises(CodingFailureClassificationError, match="typed stage"):
        _build(
            stage,
            authoring=(authoring if "authoring" in component_mode else None),
            grader=(grader if "grader" in component_mode else None),
        )


def test_repair_and_candidate_reject_resolved_grader() -> None:
    authoring, failed = _components()
    passed_groups = [
        group.model_copy(update={"passed": group.total}) for group in failed.test_groups
    ]
    resolved = failed.model_copy(update={"test_groups": passed_groups})
    assert resolved.resolved()
    for stage in (
        CodingFailureStage.REPAIR_FAILURE,
        CodingFailureStage.CANDIDATE_INTEGRITY,
    ):
        with pytest.raises(CodingFailureClassificationError, match="typed stage"):
            _build(stage, authoring=authoring, grader=resolved)


@pytest.mark.parametrize(
    ("stage", "with_grader"),
    [
        (CodingFailureStage.CANDIDATE_INTEGRITY, True),
        (CodingFailureStage.GRADING_INFRASTRUCTURE, False),
        (CodingFailureStage.REPAIR_FAILURE, True),
    ],
)
def test_post_freeze_failures_require_gradeable_authoring(
    stage: CodingFailureStage,
    with_grader: bool,
) -> None:
    authoring, grader = _components()
    authoring = authoring.model_copy(update={"protected_paths_intact": False})
    with pytest.raises(CodingFailureClassificationError, match="typed stage"):
        _build(
            stage,
            authoring=authoring,
            grader=grader if with_grader else None,
        )


@pytest.mark.parametrize(
    "drift",
    ["stage", "task", "authoring", "grader", "code", "raw_code"],
)
def test_classifier_rejects_untyped_or_authority_drift(drift: str) -> None:
    manifest = _manifest()
    selected = manifest.tasks[0]
    authoring, grader = _components()
    stage: CodingFailureStage | str = CodingFailureStage.REPAIR_FAILURE
    case_id = selected.case_id
    failure_code: CodingFailureCode | str = _CODES[CodingFailureStage.REPAIR_FAILURE]
    if drift == "stage":
        stage = "repair_failure"
    elif drift == "task":
        case_id = "unselected-case"
    elif drift == "authoring":
        model = authoring.model.model_copy(update={"inference_grant_sha256": "ff" * 32})
        authoring = authoring.model_copy(update={"model": model})
    elif drift == "grader":
        grader = grader.model_copy(update={"grader_bundle_sha256": "ff" * 32})
    elif drift == "code":
        failure_code = CodingFailureCode.TASK_MATERIAL_INVALID
    else:
        failure_code = "raw_exception_text"

    with pytest.raises(CodingFailureClassificationError, match="typed stage"):
        build_coding_failure_task_evidence(
            manifest,
            validator_ticket_id="33333333-3333-4333-8333-333333333333",
            case_id=case_id,
            variant_id=selected.variant_id,
            stage=stage,  # type: ignore[arg-type]
            failure_code=failure_code,  # type: ignore[arg-type]
            authoring=authoring,
            grader=grader,
        )
