"""Private-only compilation primitives for matched Coding Memory v2 groups."""

from __future__ import annotations

import hashlib
import hmac
import unicodedata
from dataclasses import dataclass
from typing import Literal

from dittobench_coding_datagen.canonical import canonical_json_bytes
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

    def blinded_arm_projections(
        self, *, assignment_key: bytes
    ) -> tuple[dict[str, object], ...]:
        """Return private-to-Platform arm projections, not final assignments."""

        if len(assignment_key) < 32:
            raise CorpusError("counterfactual assignment key is too short")
        return tuple(
            {
                "memory_bundle_sha256": arm.memory_bundle_sha256,
                "memory_volume_tier": arm.memory_volume_tier,
                "opaque_arm_id": hmac.new(
                    assignment_key,
                    canonical_json_bytes(
                        {
                            "condition": arm.condition,
                            "group": self.opaque_group_id,
                            "memory": arm.memory_bundle_sha256,
                            "repository_epoch": self.repository_epoch,
                        }
                    ),
                    hashlib.sha256,
                ).hexdigest(),
                "repository_epoch": self.repository_epoch,
                "seeded_memory_bytes": arm.seeded_memory_bytes,
            }
            for arm in self.arms
        )


def compile_counterfactual_group(
    *, opaque_group_id: str, repository_epoch: str, arms: tuple[CounterfactualArm, ...]
) -> CounterfactualGroup:
    """Validate a complete V0–V4 group before private catalog publication."""

    if (
        not _valid_identifier(opaque_group_id)
        or not _valid_identifier(repository_epoch)
        or len(arms) != len(_CONDITIONS)
    ):
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
        type(arm.seeded_memory_bytes) is not int
        or arm.seeded_memory_bytes <= 0
        or arm.memory_volume_tier not in _TIERS
        for arm in arms
    ):
        raise CorpusError("counterfactual memory volume is invalid")
    if len({arm.memory_volume_tier for arm in arms}) != 1:
        raise CorpusError("counterfactual group must use one memory volume tier")
    memory_digests = {arm.memory_bundle_sha256 for arm in arms}
    if len(memory_digests) != len(_CONDITIONS):
        raise CorpusError("counterfactual arms require distinct memory bundles")
    byte_counts = [arm.seeded_memory_bytes for arm in arms]
    allowed_spread = max(1024, max(byte_counts) // 20)
    if max(byte_counts) - min(byte_counts) > allowed_spread:
        raise CorpusError("counterfactual memory bundle sizes are not balanced")
    return CounterfactualGroup(
        opaque_group_id=opaque_group_id, repository_epoch=repository_epoch, arms=arms
    )


def _valid_identifier(value: str) -> bool:
    try:
        encoded = value.encode("utf-8")
    except UnicodeEncodeError:
        return False
    return (
        bool(value)
        and len(encoded) <= 256
        and not any(
            character.isspace()
            or unicodedata.category(character) in {"Cc", "Cf", "Cs", "Co"}
            for character in value
        )
    )
