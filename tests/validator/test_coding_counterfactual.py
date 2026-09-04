from __future__ import annotations

import pytest

from ditto.validator.coding_counterfactual import (
    CodingCounterfactualExecutionError,
    CounterfactualExecutionArm,
    execute_replicated_conditions,
)


def _arm(identifier: str, replicate: int) -> CounterfactualExecutionArm:
    fill = "b" if identifier == "arm-2" else "a"
    return CounterfactualExecutionArm(fill * 64, "c" * 64, replicate)


def test_counterfactual_arms_are_executed_in_unique_fresh_workspaces() -> None:
    identifiers = iter(("workspace-1", "workspace-2"))
    result = execute_replicated_conditions(
        (_arm("arm-1", 1), _arm("arm-2", 1)),
        fresh_workspace=lambda: next(identifiers),
        execute=lambda arm, workspace: (
            arm.opaque_assignment_id == "a" * 64 and workspace == "workspace-1"
        ),
    )
    assert [item.resolved for item in result] == [True, False]
    assert {item.fresh_workspace_id for item in result} == {
        "workspace-1",
        "workspace-2",
    }


def test_counterfactual_arms_reject_workspace_reuse() -> None:
    with pytest.raises(CodingCounterfactualExecutionError, match="unique fresh"):
        execute_replicated_conditions(
            (_arm("arm-1", 1), _arm("arm-2", 1)),
            fresh_workspace=lambda: "workspace",
            execute=lambda _arm, _workspace: False,
        )


def test_counterfactual_arms_reject_non_boolean_result() -> None:
    with pytest.raises(CodingCounterfactualExecutionError, match="non-boolean"):
        execute_replicated_conditions(
            (_arm("arm-1", 1),),
            fresh_workspace=lambda: "workspace",
            execute=lambda _arm, _workspace: 1,  # type: ignore[return-value]
        )
