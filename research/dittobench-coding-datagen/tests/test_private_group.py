from __future__ import annotations

import pytest

from dittobench_coding_datagen.model import CorpusError
from dittobench_coding_datagen.private_group import (
    PrivateGroupArm,
    build_private_group_manifest,
)


def _arms() -> tuple[PrivateGroupArm, ...]:
    return tuple(
        PrivateGroupArm(
            condition=condition,  # type: ignore[arg-type]
            memory_bundle_sha256=fill * 64,
            seeded_memory_bytes=4096,
            memory_volume_tier="medium",
        )
        for condition, fill in (
            ("v0_none", "a"),
            ("v1_relevant", "b"),
            ("v2_irrelevant", "c"),
            ("v3_stale_conflict", "d"),
            ("v4_current_override", "e"),
        )
    )


def test_private_group_manifest_is_complete_and_shadow_only() -> None:
    manifest = build_private_group_manifest(
        opaque_group_id="group-1",
        opaque_repository_stratum_id="stratum-1",
        repository_epoch="epoch-1",
        snapshot_manifest_sha256="a" * 64,
        visible_issue_sha256="b" * 64,
        runtime_policy_sha256="c" * 64,
        hidden_grader_sha256="d" * 64,
        resource_profile_sha256="e" * 64,
        arms=_arms(),
    )
    assert manifest.weight_eligible is False
    assert len(manifest.arms) == 5
    assert len(manifest.manifest_sha256()) == 64
    shuffled = build_private_group_manifest(
        opaque_group_id="group-1",
        opaque_repository_stratum_id="stratum-1",
        repository_epoch="epoch-1",
        snapshot_manifest_sha256="a" * 64,
        visible_issue_sha256="b" * 64,
        runtime_policy_sha256="c" * 64,
        hidden_grader_sha256="d" * 64,
        resource_profile_sha256="e" * 64,
        arms=tuple(reversed(_arms())),
    )
    assert shuffled.manifest_sha256() == manifest.manifest_sha256()
    assert [arm.condition for arm in shuffled.arms] == [
        arm.condition for arm in manifest.arms
    ]


def test_private_group_manifest_rejects_missing_condition() -> None:
    with pytest.raises(CorpusError, match="authority"):
        build_private_group_manifest(
            opaque_group_id="group-1",
            opaque_repository_stratum_id="stratum-1",
            repository_epoch="epoch-1",
            snapshot_manifest_sha256="a" * 64,
            visible_issue_sha256="b" * 64,
            runtime_policy_sha256="c" * 64,
            hidden_grader_sha256="d" * 64,
            resource_profile_sha256="e" * 64,
            arms=_arms()[:-1] + (_arms()[0],),
        )
