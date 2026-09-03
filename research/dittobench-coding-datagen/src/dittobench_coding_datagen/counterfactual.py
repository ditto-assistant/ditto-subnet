"""Private-only compilation primitives for matched Coding Memory v2 groups."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

from dittobench_coding_datagen.canonical import canonical_json_bytes, sha256_hex
from dittobench_coding_datagen.model import CorpusError

CounterfactualCondition = Literal[
    "v0_none",
    "v1_relevant",
    "v2_irrelevant",
    "v3_stale_conflict",
    "v4_current_override",
]
_CONDITIONS: frozenset[str] = frozenset(
    {
        "v0_none",
        "v1_relevant",
        "v2_irrelevant",
        "v3_stale_conflict",
        "v4_current_override",
    }
)
_TIERS: frozenset[str] = frozenset({"small", "medium", "large"})


@dataclass(frozen=True)
class CounterfactualArm:
    condition: CounterfactualCondition
    visible_bundle_sha256: str
    memory_bundle_sha256: str
    seeded_memory_bytes: int
    memory_volume_tier: Literal["small", "medium", "large"]


@dataclass(frozen=True)
class CounterfactualGroup:
    opaque_group_id: str
    repository_epoch: str
    arms: tuple[CounterfactualArm, ...]

    def miner_assignments(self) -> tuple[dict[str, object], ...]:
        """Return blinded arm projections; never include condition labels."""

        group_commitment = sha256_hex(
            canonical_json_bytes(
                {
                    "opaque_group_id": self.opaque_group_id,
                    "repository_epoch": self.repository_epoch,
                }
            )
        )
        return tuple(
            {
                "memory_bundle_sha256": arm.memory_bundle_sha256,
                "memory_volume_tier": arm.memory_volume_tier,
                "opaque_base_task_group_commitment": group_commitment,
                "opaque_variant_id": sha256_hex(
                    canonical_json_bytes(
                        {"group": group_commitment, "memory": arm.memory_bundle_sha256}
                    )
                ),
                "private_condition_commitment": sha256_hex(
                    canonical_json_bytes(
                        {"condition": arm.condition, "group": group_commitment}
                    )
                ),
                "repository_epoch": self.repository_epoch,
                "seeded_memory_bytes": arm.seeded_memory_bytes,
                "visible_bundle_sha256": arm.visible_bundle_sha256,
            }
            for arm in self.arms
        )


def compile_counterfactual_group(
    *, opaque_group_id: str, repository_epoch: str, arms: tuple[CounterfactualArm, ...]
) -> CounterfactualGroup:
    """Validate a complete V0–V4 group before private catalog publication."""

    if not opaque_group_id or not repository_epoch or len(arms) != len(_CONDITIONS):
        raise CorpusError("counterfactual group identity or arm count is invalid")
    conditions = {arm.condition for arm in arms}
    if conditions != _CONDITIONS:
        raise CorpusError("counterfactual group must contain exactly one V0–V4 arm")
    visible = {arm.visible_bundle_sha256 for arm in arms}
    if len(visible) != 1 or any(
        len(value) != 64 or any(char not in "0123456789abcdef" for char in value)
        for arm in arms
        for value in (arm.visible_bundle_sha256, arm.memory_bundle_sha256)
    ):
        raise CorpusError(
            "counterfactual group digests are invalid or visible task drifted"
        )
    if any(
        arm.seeded_memory_bytes <= 0 or arm.memory_volume_tier not in _TIERS
        for arm in arms
    ):
        raise CorpusError("counterfactual memory volume is invalid")
    return CounterfactualGroup(
        opaque_group_id=opaque_group_id, repository_epoch=repository_epoch, arms=arms
    )
