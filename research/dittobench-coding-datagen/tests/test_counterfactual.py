from __future__ import annotations

import pytest

from dittobench_coding_datagen.counterfactual import (
    CounterfactualArm,
    compile_counterfactual_group,
)
from dittobench_coding_datagen.model import CorpusError


def _arm(condition: str, memory: str) -> CounterfactualArm:
    return CounterfactualArm(
        condition=condition,  # type: ignore[arg-type]
        visible_bundle_sha256="a" * 64,
        memory_bundle_sha256=memory * 64,
        seeded_memory_bytes=1024,
        memory_volume_tier="medium",
    )


def test_compiler_emits_blinded_complete_group() -> None:
    group = compile_counterfactual_group(
        opaque_group_id="opaque-group-1",
        repository_epoch="repo@abc",
        arms=tuple(
            _arm(condition, memory)
            for condition, memory in zip(
                (
                    "v0_none",
                    "v1_relevant",
                    "v2_irrelevant",
                    "v3_stale_conflict",
                    "v4_current_override",
                ),
                ("b", "c", "d", "e", "f"),
                strict=True,
            )
        ),
    )
    assignments = group.miner_assignments()
    assert len(assignments) == 5
    assert all("condition" not in assignment for assignment in assignments)
    assert (
        len(
            {
                assignment["opaque_base_task_group_commitment"]
                for assignment in assignments
            }
        )
        == 1
    )


def test_compiler_rejects_incomplete_or_visible_drifting_groups() -> None:
    with pytest.raises(CorpusError, match="arm count"):
        compile_counterfactual_group(
            opaque_group_id="opaque-group-1",
            repository_epoch="repo@abc",
            arms=(_arm("v0_none", "b"),),
        )
    with pytest.raises(CorpusError, match="visible task drifted"):
        compile_counterfactual_group(
            opaque_group_id="opaque-group-1",
            repository_epoch="repo@abc",
            arms=(
                _arm("v0_none", "b"),
                _arm("v1_relevant", "c"),
                _arm("v2_irrelevant", "d"),
                _arm("v3_stale_conflict", "e"),
                CounterfactualArm(
                    condition="v4_current_override",
                    visible_bundle_sha256="f" * 64,
                    memory_bundle_sha256="g" * 64,
                    seeded_memory_bytes=1024,
                    memory_volume_tier="medium",
                ),
            ),
        )
