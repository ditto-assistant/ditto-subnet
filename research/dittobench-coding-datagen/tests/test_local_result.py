from __future__ import annotations

import json
from dataclasses import replace

import pytest

from dittobench_coding_datagen.local_result import (
    LocalPracticeTaskResult,
    build_local_practice_result,
)
from dittobench_coding_datagen.model import CorpusError


def _tasks() -> tuple[LocalPracticeTaskResult, ...]:
    conditions = (
        "v0_none",
        "v0_none",
        "v1_relevant",
        "v1_relevant",
        "v2_irrelevant",
        "v2_irrelevant",
        "v3_stale_conflict",
        "v3_stale_conflict",
        "v4_current_override",
        "v4_current_override",
    )
    return tuple(
        LocalPracticeTaskResult(
            task_id=f"PUBLIC-V2-{index:02d}",
            condition=condition,  # type: ignore[arg-type]
            resolved=index % 2 == 0,
            protocol_valid=True,
            patch_valid=True,
            terminal_domain="resolved" if index % 2 == 0 else "repair_failure",
        )
        for index, condition in enumerate(conditions)
    )


def test_local_result_is_canonical_and_permanently_non_authoritative() -> None:
    result = build_local_practice_result(
        public_release_id="coding-public-v2",
        public_release_manifest_sha256="a" * 64,
        harness_artifact_sha256="b" * 64,
        tasks=_tasks(),
    )
    assert result.resolved_count == 5
    assert result.local_practice_score_micros == 500_000
    assert result.authoritative is False
    assert result.leaderboard_eligible is False
    assert result.reward_eligible is False
    assert json.loads(result.canonical_bytes())["tasks_total"] == 10


def test_local_result_rejects_unbalanced_conditions() -> None:
    invalid = _tasks()[:-1] + (replace(_tasks()[0], task_id="PUBLIC-V2-10"),)
    with pytest.raises(CorpusError, match="two tasks"):
        build_local_practice_result(
            public_release_id="coding-public-v2",
            public_release_manifest_sha256="a" * 64,
            harness_artifact_sha256="b" * 64,
            tasks=invalid,
        )


def test_local_result_sorts_tasks_and_rejects_path_ids() -> None:
    shuffled = tuple(reversed(_tasks()))
    first = build_local_practice_result(
        public_release_id="coding-public-v2",
        public_release_manifest_sha256="a" * 64,
        harness_artifact_sha256="b" * 64,
        tasks=shuffled,
    )
    second = build_local_practice_result(
        public_release_id="coding-public-v2",
        public_release_manifest_sha256="a" * 64,
        harness_artifact_sha256="b" * 64,
        tasks=_tasks(),
    )
    assert first.canonical_bytes() == second.canonical_bytes()
    assert [task.task_id for task in first.tasks] == sorted(
        task.task_id for task in shuffled
    )
    with pytest.raises(CorpusError, match="authority is invalid"):
        build_local_practice_result(
            public_release_id="coding-public-v2",
            public_release_manifest_sha256="a" * 64,
            harness_artifact_sha256="b" * 64,
            tasks=(replace(_tasks()[0], task_id="../escape"),) + _tasks()[1:],
        )


def test_local_result_rejects_resolved_without_valid_patch() -> None:
    invalid = (replace(_tasks()[0], resolved=True, patch_valid=False),) + _tasks()[1:]
    with pytest.raises(CorpusError, match="authority is invalid"):
        build_local_practice_result(
            public_release_id="coding-public-v2",
            public_release_manifest_sha256="a" * 64,
            harness_artifact_sha256="b" * 64,
            tasks=invalid,
        )
