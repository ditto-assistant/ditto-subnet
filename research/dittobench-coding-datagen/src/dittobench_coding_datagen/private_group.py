"""Opaque private Coding group manifests; no private source bytes are accepted."""

from __future__ import annotations

import unicodedata
from dataclasses import dataclass
from typing import Literal

from dittobench_coding_datagen.canonical import canonical_json_bytes, sha256_hex
from dittobench_coding_datagen.model import CorpusError

PrivateCondition = Literal[
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


@dataclass(frozen=True)
class PrivateGroupArm:
    condition: PrivateCondition
    memory_bundle_sha256: str
    seeded_memory_bytes: int
    memory_volume_tier: Literal["small", "medium", "large"]


@dataclass(frozen=True)
class PrivateGroupManifest:
    schema: str
    opaque_group_id: str
    opaque_repository_stratum_id: str
    repository_epoch: str
    snapshot_manifest_sha256: str
    visible_issue_sha256: str
    runtime_policy_sha256: str
    hidden_grader_sha256: str
    resource_profile_sha256: str
    arms: tuple[PrivateGroupArm, ...]
    weight_eligible: bool

    def as_json(self) -> dict[str, object]:
        return {
            "arms": [
                {
                    "condition": arm.condition,
                    "memory_bundle_sha256": arm.memory_bundle_sha256,
                    "memory_volume_tier": arm.memory_volume_tier,
                    "seeded_memory_bytes": arm.seeded_memory_bytes,
                }
                for arm in sorted(self.arms, key=lambda arm: arm.condition)
            ],
            "hidden_grader_sha256": self.hidden_grader_sha256,
            "opaque_group_id": self.opaque_group_id,
            "opaque_repository_stratum_id": self.opaque_repository_stratum_id,
            "repository_epoch": self.repository_epoch,
            "resource_profile_sha256": self.resource_profile_sha256,
            "runtime_policy_sha256": self.runtime_policy_sha256,
            "schema": self.schema,
            "snapshot_manifest_sha256": self.snapshot_manifest_sha256,
            "visible_issue_sha256": self.visible_issue_sha256,
            "weight_eligible": self.weight_eligible,
        }

    def canonical_bytes(self) -> bytes:
        return canonical_json_bytes(self.as_json())

    def manifest_sha256(self) -> str:
        return sha256_hex(self.canonical_bytes())


def build_private_group_manifest(
    *,
    opaque_group_id: str,
    opaque_repository_stratum_id: str,
    repository_epoch: str,
    snapshot_manifest_sha256: str,
    visible_issue_sha256: str,
    runtime_policy_sha256: str,
    hidden_grader_sha256: str,
    resource_profile_sha256: str,
    arms: tuple[PrivateGroupArm, ...],
) -> PrivateGroupManifest:
    """Bind exactly one complete V0-V4 group without source provenance."""

    if (
        not all(
            _identifier(value)
            for value in (
                opaque_group_id,
                opaque_repository_stratum_id,
                repository_epoch,
            )
        )
        or any(
            not _sha256(value)
            for value in (
                snapshot_manifest_sha256,
                visible_issue_sha256,
                runtime_policy_sha256,
                hidden_grader_sha256,
                resource_profile_sha256,
            )
        )
        or len(arms) != 5
        or {arm.condition for arm in arms} != _CONDITIONS
        or len({arm.memory_bundle_sha256 for arm in arms}) != 5
        or len({arm.memory_volume_tier for arm in arms}) != 1
        or any(
            not _sha256(arm.memory_bundle_sha256)
            or type(arm.seeded_memory_bytes) is not int
            or arm.seeded_memory_bytes <= 0
            or arm.memory_volume_tier not in {"small", "medium", "large"}
            for arm in arms
        )
    ):
        raise CorpusError("private group manifest authority is invalid")
    byte_counts = [arm.seeded_memory_bytes for arm in arms]
    if max(byte_counts) - min(byte_counts) > max(64, max(byte_counts) // 20):
        raise CorpusError("private group memory bundle sizes are not balanced")
    return PrivateGroupManifest(
        schema="dittobench-coding-private-group-v2",
        opaque_group_id=opaque_group_id,
        opaque_repository_stratum_id=opaque_repository_stratum_id,
        repository_epoch=repository_epoch,
        snapshot_manifest_sha256=snapshot_manifest_sha256,
        visible_issue_sha256=visible_issue_sha256,
        runtime_policy_sha256=runtime_policy_sha256,
        hidden_grader_sha256=hidden_grader_sha256,
        resource_profile_sha256=resource_profile_sha256,
        arms=tuple(sorted(arms, key=lambda arm: arm.condition)),
        weight_eligible=False,
    )


def _identifier(value: str) -> bool:
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


def _sha256(value: str) -> bool:
    return len(value) == 64 and all(
        character in "0123456789abcdef" for character in value
    )


__all__ = ["PrivateGroupArm", "PrivateGroupManifest", "build_private_group_manifest"]
