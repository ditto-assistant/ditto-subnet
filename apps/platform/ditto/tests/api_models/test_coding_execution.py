"""Platform parity and privacy tests for private coding execution plans."""

from __future__ import annotations

import json
from copy import deepcopy
from pathlib import Path

import pytest
from pydantic import ValidationError

from ditto.api_models.coding_selection import (
    CodingCatalogGraderPlan,
    CodingCatalogResourceProfile,
    CodingCatalogRunnerPlan,
    CodingCatalogRuntimePolicy,
    coding_catalog_grader_plan_digest,
    coding_catalog_resource_profile_digest,
    coding_catalog_runner_plan_digest,
    validate_coding_catalog_execution_bundle,
)

_VECTOR_PATH = (
    Path(__file__).parents[5]
    / "packages"
    / "dittobench-coding-contract"
    / "testdata"
    / "coding_execution_plan_v1.json"
)


def _vector() -> dict:
    return json.loads(_VECTOR_PATH.read_text(encoding="utf-8"))


def test_platform_recomputes_shared_execution_plan_authority() -> None:
    vector = _vector()
    runner = CodingCatalogRunnerPlan.model_validate(vector["runner_plan"])
    runtime = CodingCatalogRuntimePolicy.model_validate(vector["runtime_policy"])
    grader = CodingCatalogGraderPlan.model_validate(vector["grader_plan"])
    resource = CodingCatalogResourceProfile.model_validate(
        vector["grader_resource_profile"]
    )
    assert (
        coding_catalog_runner_plan_digest(runner)
        == vector["expected"]["runner_plan_sha256"]
    )
    assert (
        coding_catalog_grader_plan_digest(grader)
        == vector["expected"]["grader_plan_sha256"]
    )
    assert (
        coding_catalog_resource_profile_digest(resource)
        == vector["expected"]["grader_resource_profile_sha256"]
    )
    validate_coding_catalog_execution_bundle(
        runner_plan=runner,
        runtime_policy=runtime,
        grader_plan=grader,
        resource_profile=resource,
    )
    assert "grader/hidden.py" not in repr(grader)


def test_platform_execution_plan_projection_ignores_unknown_fields() -> None:
    vector = _vector()
    original = CodingCatalogRunnerPlan.model_validate(vector["runner_plan"])
    extended = deepcopy(vector["runner_plan"])
    extended["future_diagnostic"] = {"ignored": True}
    extended["test_commands"][0]["future_hint"] = "ignored"
    parsed = CodingCatalogRunnerPlan.model_validate(extended)
    assert coding_catalog_runner_plan_digest(
        parsed
    ) == coding_catalog_runner_plan_digest(original)


@pytest.mark.parametrize(
    "mutation",
    ["runtime", "runner", "grader", "resource", "shell", "oversized", "overlap"],
)
def test_platform_execution_plan_drift_fails_closed(mutation: str) -> None:
    vector = _vector()
    runner = deepcopy(vector["runner_plan"])
    runtime = deepcopy(vector["runtime_policy"])
    grader = deepcopy(vector["grader_plan"])
    resource = deepcopy(vector["grader_resource_profile"])
    if mutation == "runtime":
        runtime["test_command_ids"] = []
    elif mutation == "runner":
        runner["limits"]["max_workspace_bytes"] = 2 << 30
    elif mutation == "grader":
        grader["case_id"] = "other-case"
    elif mutation == "resource":
        resource["memory_limit_bytes"] += 1
    elif mutation == "shell":
        runner["test_commands"][0]["argv"] = ["sh", "-c", "pytest"]
    elif mutation == "oversized":
        runner["test_commands"][0]["argv"] = [
            "python",
            "x" * 4096,
            "y" * 4096,
        ]
    elif mutation == "overlap":
        runner["creatable_paths"] = [runner["editable_paths"][0]]
    with pytest.raises((ValidationError, ValueError)):
        validate_coding_catalog_execution_bundle(
            runner_plan=CodingCatalogRunnerPlan.model_validate(runner),
            runtime_policy=CodingCatalogRuntimePolicy.model_validate(runtime),
            grader_plan=CodingCatalogGraderPlan.model_validate(grader),
            resource_profile=CodingCatalogResourceProfile.model_validate(resource),
        )
