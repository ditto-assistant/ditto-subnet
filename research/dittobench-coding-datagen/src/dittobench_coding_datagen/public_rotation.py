"""Deterministic public/held-out rotation for shadow coding datasets.

The router-and-compression competition grades routers on a public pack the miner
can see plus a disjoint held-out partition that is withheld and must rotate over
time so a router cannot be tuned to a fixed hidden set. This module owns that
rotation.

The seed-mixing avalanche is a byte-for-byte mirror of ``RotateSeedForVersion``
in ``research/dittobench-datagen/protocol/epoch.go`` (splitmix64 finalizer folded
with the golden-ratio gamma). Keeping the two implementations identical means an
auditor can recompute a rotation from either the Go generator or this Python
research adapter and get the same partition. The partition is a pure function of
``(task_ids, base_seed, version)`` and never touches held-out bytes.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass

from dittobench_coding_datagen.model import CorpusError

_U64_MASK = (1 << 64) - 1
_I64_MIN = -(1 << 63)
_I64_MAX = (1 << 63) - 1

# splitmix64 constants, identical to protocol/epoch.go.
_GOLDEN_GAMMA = 0x9E3779B97F4A7C15
_MIX_A = 0xBF58476D1CE4E5B9
_MIX_B = 0x94D049BB133111EB

# A rotation version is a monotonically advancing dataset epoch. Bound it so a
# corrupt manifest cannot request an absurd version that would look valid.
MIN_ROTATION_VERSION = 1
MAX_ROTATION_VERSION = 1 << 20


def _to_int64(value: int) -> int:
    value &= _U64_MASK
    return value - (1 << 64) if value >= (1 << 63) else value


def _splitmix64(value: int) -> int:
    """The splitmix64 finalizer over an unsigned 64-bit word."""
    value &= _U64_MASK
    value ^= value >> 30
    value = (value * _MIX_A) & _U64_MASK
    value ^= value >> 27
    value = (value * _MIX_B) & _U64_MASK
    value ^= value >> 31
    return value & _U64_MASK


def rotate_seed_for_version(seed: int, version: int) -> int:
    """Fold a rotation version into a base seed.

    Returns a signed 64-bit integer identical to ``RotateSeedForVersion`` in
    ``protocol/epoch.go`` so a partition is reproducible from either generator.
    """
    if isinstance(seed, bool) or isinstance(version, bool):
        raise CorpusError("rotation seed and version must be integers, not bool")
    if not isinstance(seed, int) or not isinstance(version, int):
        raise CorpusError("rotation seed and version must be integers")
    if not MIN_ROTATION_VERSION <= version <= MAX_ROTATION_VERSION:
        raise CorpusError(
            f"rotation version {version} is outside "
            f"[{MIN_ROTATION_VERSION}, {MAX_ROTATION_VERSION}]"
        )
    if not _I64_MIN <= seed <= _I64_MAX:
        raise CorpusError("rotation base seed does not fit in a signed 64-bit range")
    folded = (seed & _U64_MASK) ^ ((version & _U64_MASK) * _GOLDEN_GAMMA & _U64_MASK)
    return _to_int64(_splitmix64(folded))


def _task_fold(task_id: str) -> int:
    """Stable unsigned 64-bit fold of a task id (order-independent of insertion)."""
    digest = hashlib.sha256(task_id.encode("utf-8")).digest()
    return int.from_bytes(digest[:8], "big")


def _partition_key(task_id: str, rotated_seed: int, version: int) -> int:
    """Per-task ranking key that changes with the rotation version."""
    mixed = (
        _task_fold(task_id)
        ^ (rotated_seed & _U64_MASK)
        ^ ((version & _U64_MASK) * _GOLDEN_GAMMA & _U64_MASK)
    )
    return _splitmix64(mixed)


@dataclass(frozen=True)
class DatasetRotation:
    """A deterministic split of a dataset into a visible and a held-out partition.

    ``public_task_ids`` is the miner-visible pack; ``heldout_task_ids`` is the
    withheld partition that must be authored through the shadow-only private
    group path (``private_group.build_private_group_manifest``, whose
    ``weight_eligible`` is permanently ``False``). The two tuples are disjoint,
    sorted, and together cover every input task exactly once.
    """

    version: int
    rotated_seed: int
    public_task_ids: tuple[str, ...]
    heldout_task_ids: tuple[str, ...]

    def __post_init__(self) -> None:
        public = set(self.public_task_ids)
        heldout = set(self.heldout_task_ids)
        if public & heldout:
            raise CorpusError("rotation partitions overlap")
        if len(public) != len(self.public_task_ids):
            raise CorpusError("public partition has duplicate task ids")
        if len(heldout) != len(self.heldout_task_ids):
            raise CorpusError("held-out partition has duplicate task ids")


def rotate_dataset_partition(
    *,
    task_ids: tuple[str, ...],
    base_seed: int,
    version: int,
    heldout_size: int,
) -> DatasetRotation:
    """Partition ``task_ids`` into a public pack and a rotating held-out set.

    The held-out membership is a deterministic function of the rotation version,
    so advancing ``version`` reshuffles which tasks are withheld without ever
    exposing held-out bytes. Ties are broken by task id so the result is stable
    regardless of the input ordering.
    """
    if not task_ids:
        raise CorpusError("rotation requires at least one task id")
    ordered = tuple(sorted(task_ids))
    if len(set(ordered)) != len(ordered):
        raise CorpusError("rotation task ids must be unique")
    for task_id in ordered:
        if not task_id or not task_id.strip():
            raise CorpusError("rotation task ids must be non-empty")
    if not isinstance(heldout_size, int) or isinstance(heldout_size, bool):
        raise CorpusError("held-out size must be an integer")
    if not 1 <= heldout_size < len(ordered):
        raise CorpusError(
            f"held-out size {heldout_size} must be in [1, {len(ordered) - 1}] "
            "so both partitions are non-empty"
        )

    rotated_seed = rotate_seed_for_version(base_seed, version)
    ranked = sorted(
        ordered,
        key=lambda task_id: (_partition_key(task_id, rotated_seed, version), task_id),
    )
    heldout = tuple(sorted(ranked[:heldout_size]))
    public = tuple(sorted(ranked[heldout_size:]))
    return DatasetRotation(
        version=version,
        rotated_seed=rotated_seed,
        public_task_ids=public,
        heldout_task_ids=heldout,
    )


__all__ = [
    "MAX_ROTATION_VERSION",
    "MIN_ROTATION_VERSION",
    "DatasetRotation",
    "rotate_dataset_partition",
    "rotate_seed_for_version",
]
