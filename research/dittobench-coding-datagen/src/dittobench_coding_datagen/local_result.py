"""Canonical, non-authoritative aggregate results for public Coding practice."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

from dittobench_coding_datagen.canonical import canonical_json_bytes
from dittobench_coding_datagen.model import CorpusError

LocalPracticeCondition = Literal[
    "v0_none",
    "v1_relevant",
    "v2_irrelevant",
    "v3_stale_conflict",
    "v4_current_override",
]

_CONDITIONS: tuple[LocalPracticeCondition, ...] = (
    "v0_none",
    "v1_relevant",
    "v2_irrelevant",
    "v3_stale_conflict",
    "v4_current_override",
)


@dataclass(frozen=True)
class LocalPracticeTaskResult:
    """One locally observed public task outcome; never score authority."""

    task_id: str
    condition: LocalPracticeCondition
    resolved: bool
    protocol_valid: bool
    patch_valid: bool
    terminal_domain: Literal["resolved", "repair_failure", "harness_failure"]

    def as_json(self) -> dict[str, object]:
        return {
            "condition": self.condition,
            "patch_valid": self.patch_valid,
            "protocol_valid": self.protocol_valid,
            "resolved": self.resolved,
            "task_id": self.task_id,
            "terminal_domain": self.terminal_domain,
        }


@dataclass(frozen=True)
class LocalPracticeResult:
    """Ten-task public report with permanent non-authoritative markings."""

    schema: str
    coding_contract_version: int
    public_release_id: str
    public_release_manifest_sha256: str
    harness_artifact_sha256: str
    tasks: tuple[LocalPracticeTaskResult, ...]
    resolved_count: int
    local_practice_score_micros: int
    authoritative: bool
    leaderboard_eligible: bool
    reward_eligible: bool

    def as_json(self) -> dict[str, object]:
        return {
            "authoritative": self.authoritative,
            "coding_contract_version": self.coding_contract_version,
            "harness_artifact_sha256": self.harness_artifact_sha256,
            "leaderboard_eligible": self.leaderboard_eligible,
            "local_practice_score_micros": self.local_practice_score_micros,
            "public_release_id": self.public_release_id,
            "public_release_manifest_sha256": self.public_release_manifest_sha256,
            "resolved_count": self.resolved_count,
            "reward_eligible": self.reward_eligible,
            "schema": self.schema,
            "tasks": [task.as_json() for task in self.tasks],
            "tasks_total": len(self.tasks),
        }

    def canonical_bytes(self) -> bytes:
        return canonical_json_bytes(self.as_json())


def build_local_practice_result(
    *,
    public_release_id: str,
    public_release_manifest_sha256: str,
    harness_artifact_sha256: str,
    tasks: tuple[LocalPracticeTaskResult, ...],
) -> LocalPracticeResult:
    """Build one strict ten-task report without granting competitive authority."""

    if (
        not _safe_identifier(public_release_id)
        or not _sha256(public_release_manifest_sha256)
        or not _sha256(harness_artifact_sha256)
        or len(tasks) != 10
        or len({task.task_id for task in tasks}) != 10
        or any(not _safe_identifier(task.task_id) for task in tasks)
        or any(type(task.resolved) is not bool for task in tasks)
        or any(type(task.protocol_valid) is not bool for task in tasks)
        or any(type(task.patch_valid) is not bool for task in tasks)
    ):
        raise CorpusError("local practice result authority is invalid")
    for condition in _CONDITIONS:
        if sum(task.condition == condition for task in tasks) != 2:
            raise CorpusError(
                "local practice result must contain two tasks per condition"
            )
    if any((task.terminal_domain == "resolved") != task.resolved for task in tasks):
        raise CorpusError("local practice result terminal domain disagrees")
    resolved_count = sum(task.resolved for task in tasks)
    return LocalPracticeResult(
        schema="dittobench-coding-local-practice-result-v2",
        coding_contract_version=2,
        public_release_id=public_release_id,
        public_release_manifest_sha256=public_release_manifest_sha256,
        harness_artifact_sha256=harness_artifact_sha256,
        tasks=tasks,
        resolved_count=resolved_count,
        local_practice_score_micros=(resolved_count * 1_000_000) // len(tasks),
        authoritative=False,
        leaderboard_eligible=False,
        reward_eligible=False,
    )


def _sha256(value: str) -> bool:
    return len(value) == 64 and all(
        character in "0123456789abcdef" for character in value
    )


def _safe_identifier(value: str) -> bool:
    return (
        bool(value)
        and len(value.encode("utf-8")) <= 256
        and not any(character.isspace() or ord(character) < 32 for character in value)
    )


__all__ = [
    "LocalPracticeCondition",
    "LocalPracticeResult",
    "LocalPracticeTaskResult",
    "build_local_practice_result",
]
