from __future__ import annotations

import pytest

from dittobench_coding_datagen.model import CorpusError
from dittobench_coding_datagen.private_group import (
    PrivateGroupArm,
    build_private_group_manifest,
)
from dittobench_coding_datagen.public_rotation import (
    DatasetRotation,
    rotate_dataset_partition,
    rotate_seed_for_version,
)

# Golden vectors recomputed independently from the Go generator
# (research/dittobench-datagen/protocol/epoch.go, RotateSeedForVersion). If either
# implementation drifts these must be updated together, and a drift means a
# historical rotation would no longer reproduce.
_SEED_VECTORS = {
    (0, 2): 7960286522194355700,
    (0, 3): 487617019471545679,
    (1, 1): -1956407806741107680,
    (1, 2): -4689498862643123097,
    (1234567890123, 7): -2126205754726593004,
    (-1, 5): 5549181937935055082,
}

_TASKS = tuple(f"PUBLIC-V2-{index:02d}" for index in range(10))
_PARTITION_VECTORS = {
    1: ("PUBLIC-V2-00", "PUBLIC-V2-01", "PUBLIC-V2-02", "PUBLIC-V2-03"),
    2: ("PUBLIC-V2-03", "PUBLIC-V2-04", "PUBLIC-V2-06", "PUBLIC-V2-09"),
    3: ("PUBLIC-V2-03", "PUBLIC-V2-04", "PUBLIC-V2-08", "PUBLIC-V2-09"),
}


def test_rotate_seed_matches_go_golden_vectors() -> None:
    for (seed, version), expected in _SEED_VECTORS.items():
        assert rotate_seed_for_version(seed, version) == expected


def test_rotate_seed_is_deterministic_and_active() -> None:
    assert rotate_seed_for_version(123456789, 4) == rotate_seed_for_version(
        123456789, 4
    )
    # The version rotation must actually move the seed.
    assert rotate_seed_for_version(123456789, 4) != 123456789
    assert rotate_seed_for_version(0, 2) != rotate_seed_for_version(0, 3)


def test_rotate_seed_rejects_out_of_range_version() -> None:
    with pytest.raises(CorpusError, match="version"):
        rotate_seed_for_version(0, 0)
    with pytest.raises(CorpusError, match="version"):
        rotate_seed_for_version(0, (1 << 20) + 1)


def test_rotate_seed_rejects_non_integer_inputs() -> None:
    with pytest.raises(CorpusError, match="integers"):
        rotate_seed_for_version(True, 1)  # type: ignore[arg-type]
    with pytest.raises(CorpusError, match="integers"):
        rotate_seed_for_version(0, "2")  # type: ignore[arg-type]


def test_partition_matches_golden_vectors() -> None:
    for version, heldout in _PARTITION_VECTORS.items():
        rotation = rotate_dataset_partition(
            task_ids=_TASKS,
            base_seed=1234567890123,
            version=version,
            heldout_size=4,
        )
        assert rotation.heldout_task_ids == heldout
        # The two partitions cover every task exactly once and never overlap.
        assert set(rotation.public_task_ids) | set(heldout) == set(_TASKS)
        assert not set(rotation.public_task_ids) & set(heldout)


def test_partition_is_stable_regardless_of_input_order() -> None:
    forward = rotate_dataset_partition(
        task_ids=_TASKS, base_seed=99, version=5, heldout_size=3
    )
    reversed_input = rotate_dataset_partition(
        task_ids=tuple(reversed(_TASKS)), base_seed=99, version=5, heldout_size=3
    )
    assert forward == reversed_input


def test_partition_held_out_set_rotates_across_versions() -> None:
    seen: set[tuple[str, ...]] = set()
    for version in range(1, 12):
        rotation = rotate_dataset_partition(
            task_ids=_TASKS, base_seed=77, version=version, heldout_size=4
        )
        seen.add(rotation.heldout_task_ids)
    # Rotation must expose more than one held-out membership over the epochs so a
    # miner cannot tune a router against a fixed hidden set.
    assert len(seen) > 1


def test_partition_rejects_degenerate_sizes() -> None:
    with pytest.raises(CorpusError, match="held-out size"):
        rotate_dataset_partition(
            task_ids=_TASKS, base_seed=1, version=1, heldout_size=0
        )
    with pytest.raises(CorpusError, match="held-out size"):
        rotate_dataset_partition(
            task_ids=_TASKS, base_seed=1, version=1, heldout_size=len(_TASKS)
        )


def test_partition_rejects_duplicate_task_ids() -> None:
    with pytest.raises(CorpusError, match="unique"):
        rotate_dataset_partition(
            task_ids=("A", "A", "B"), base_seed=1, version=1, heldout_size=1
        )


def test_dataset_rotation_dataclass_rejects_overlap() -> None:
    with pytest.raises(CorpusError, match="overlap"):
        DatasetRotation(
            version=1,
            rotated_seed=0,
            public_task_ids=("A", "B"),
            heldout_task_ids=("B", "C"),
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
            ("v0_none", "0"),
            ("v1_relevant", "1"),
            ("v2_irrelevant", "2"),
            ("v3_stale_conflict", "3"),
            ("v4_current_override", "4"),
        )
    )


def test_held_out_partition_routes_through_shadow_only_private_group() -> None:
    # The held-out side of a rotation must be authored as a private group, whose
    # weight_eligible is permanently False, so a rotating hidden set can never
    # influence public leaderboard weights.
    rotation = rotate_dataset_partition(
        task_ids=_TASKS, base_seed=1234567890123, version=2, heldout_size=4
    )
    manifest = build_private_group_manifest(
        opaque_group_id="heldout-epoch-2",
        opaque_repository_stratum_id="stratum-1",
        repository_epoch="epoch-2",
        snapshot_manifest_sha256="a" * 64,
        visible_issue_sha256="b" * 64,
        runtime_policy_sha256="c" * 64,
        hidden_grader_sha256="d" * 64,
        resource_profile_sha256="e" * 64,
        arms=_arms(),
    )
    assert rotation.heldout_task_ids
    assert manifest.weight_eligible is False
