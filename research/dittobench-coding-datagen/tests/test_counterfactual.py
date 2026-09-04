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
    assignments = group.blinded_arm_projections(assignment_key=b"k" * 32)
    assert len(assignments) == 5
    assert all(
        forbidden not in assignment
        for assignment in assignments
        for forbidden in (
            "condition",
            "opaque_base_task_group_commitment",
            "private_condition_commitment",
        )
    )
    assert len({assignment["opaque_arm_id"] for assignment in assignments}) == 5
    assert all("visible_bundle_sha256" not in assignment for assignment in assignments)
    assert assignments != group.blinded_arm_projections(assignment_key=b"z" * 32)


def test_compiler_rejects_short_assignment_key() -> None:
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
    with pytest.raises(CorpusError, match="key is too short"):
        group.blinded_arm_projections(assignment_key=b"short")


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


def test_compiler_rejects_unbalanced_bundle_sizes() -> None:
    arms = [
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
    ]
    arms[-1] = CounterfactualArm(
        condition="v4_current_override",
        visible_bundle_sha256="a" * 64,
        memory_bundle_sha256="f" * 64,
        seeded_memory_bytes=4096,
        memory_volume_tier="medium",
    )
    with pytest.raises(CorpusError, match="sizes are not balanced"):
        compile_counterfactual_group(
            opaque_group_id="opaque-group-1",
            repository_epoch="repo@abc",
            arms=tuple(arms),
        )


def test_compiler_rejects_boolean_seeded_memory_bytes() -> None:
    with pytest.raises(CorpusError, match="volume"):
        compile_counterfactual_group(
            opaque_group_id="opaque-group-1",
            repository_epoch="repo@abc",
            arms=tuple(
                CounterfactualArm(
                    condition=condition,  # type: ignore[arg-type]
                    visible_bundle_sha256="a" * 64,
                    memory_bundle_sha256=memory * 64,
                    seeded_memory_bytes=True,  # type: ignore[arg-type]
                    memory_volume_tier="medium",
                )
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


def test_compiler_rejects_unsafe_group_identity() -> None:
    with pytest.raises(CorpusError, match="identity"):
        compile_counterfactual_group(
            opaque_group_id="g\u200b1",
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
