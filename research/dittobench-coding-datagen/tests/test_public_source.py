from __future__ import annotations

import json
from pathlib import Path

import pytest

from dittobench_coding_datagen.model import CorpusError
from dittobench_coding_datagen.public_source import load_public_source_intake


def _task(index: int, language: str, condition: str) -> dict[str, object]:
    return {
        "task_id": f"PUBLIC-V2-{index:02d}",
        "repository_family": {
            "python": "python-family",
            "typescript": "typescript-family",
            "rust": "rust-family",
            "go": "go-family",
        }[language],
        "language": language,
        "licence_spdx": "MIT",
        "source_kind": "swe_bench_verified"
        if language == "python"
        else "swe_bench_multilingual",
        "public_issue_url": f"https://github.com/example/{language}/issues/{index}",
        "source_snapshot_manifest_sha256": "a" * 64,
        "source_snapshot_archive_sha256": "b" * 64,
        "visible_grader_sha256": "c" * 64,
        "condition": condition,
    }


def _manifest() -> dict[str, object]:
    layout = (
        ("python", "v0_none"),
        ("python", "v0_none"),
        ("python", "v1_relevant"),
        ("typescript", "v1_relevant"),
        ("typescript", "v2_irrelevant"),
        ("typescript", "v2_irrelevant"),
        ("rust", "v3_stale_conflict"),
        ("rust", "v3_stale_conflict"),
        ("go", "v4_current_override"),
        ("go", "v4_current_override"),
    )
    return {
        "schema": "dittobench-coding-public-source-intake-v2",
        "public_release_id": "coding-public-v2",
        "tasks": [
            _task(index, language, condition)
            for index, (language, condition) in enumerate(layout)
        ],
    }


def test_public_source_intake_requires_ten_balanced_tasks(tmp_path: Path) -> None:
    path = tmp_path / "intake.json"
    path.write_bytes(json.dumps(_manifest()).encode("utf-8"))
    intake = load_public_source_intake(path)

    assert len(intake.tasks) == 10
    assert intake.canonical_bytes().endswith(b"\n")


def test_public_source_intake_rejects_non_public_source_urls(tmp_path: Path) -> None:
    value = _manifest()
    value["tasks"][0]["public_issue_url"] = "https://private.invalid/issue"  # type: ignore[index]
    path = tmp_path / "intake.json"
    path.write_bytes(json.dumps(value).encode("utf-8"))
    with pytest.raises(CorpusError, match="authority"):
        load_public_source_intake(path)


def test_public_source_intake_rejects_path_task_ids(tmp_path: Path) -> None:
    value = _manifest()
    value["tasks"][0]["task_id"] = "../etc"  # type: ignore[index]
    path = tmp_path / "intake.json"
    path.write_bytes(json.dumps(value).encode("utf-8"))
    with pytest.raises(CorpusError, match="authority"):
        load_public_source_intake(path)


_CONDITION_CYCLE = (
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


def _function_manifest() -> dict[str, object]:
    tasks = []
    for index, condition in enumerate(_CONDITION_CYCLE):
        humaneval = index % 2 == 0
        tasks.append(
            {
                "task_id": f"FUNC-V2-{index:02d}",
                "repository_family": (
                    "openai-human-eval" if humaneval else "google-research-mbpp"
                ),
                "language": "python",
                "licence_spdx": "MIT" if humaneval else "CC-BY-4.0",
                "source_kind": "humaneval" if humaneval else "mbpp",
                "public_issue_url": f"https://github.com/example/func/issues/{index}",
                "source_snapshot_manifest_sha256": "a" * 64,
                "source_snapshot_archive_sha256": "b" * 64,
                "visible_grader_sha256": "c" * 64,
                "condition": condition,
            }
        )
    return {
        "schema": "dittobench-coding-public-source-intake-v2",
        "public_release_id": "coding-public-func-v2",
        "tasks": tasks,
    }


def test_public_source_intake_accepts_function_scoped_open_datasets(
    tmp_path: Path,
) -> None:
    path = tmp_path / "intake.json"
    path.write_bytes(json.dumps(_function_manifest()).encode("utf-8"))
    intake = load_public_source_intake(path)

    kinds = {task.source_kind for task in intake.tasks}
    assert kinds == {"humaneval", "mbpp"}
    assert all(task.language == "python" for task in intake.tasks)


def test_public_source_intake_accepts_swe_bench_lite(tmp_path: Path) -> None:
    value = _manifest()
    value["tasks"][0]["source_kind"] = "swe_bench_lite"  # type: ignore[index]
    path = tmp_path / "intake.json"
    path.write_bytes(json.dumps(value).encode("utf-8"))
    intake = load_public_source_intake(path)

    assert intake.tasks[0].source_kind == "swe_bench_lite"


def test_public_source_intake_rejects_licence_not_allowed_for_kind(
    tmp_path: Path,
) -> None:
    value = _manifest()
    value["tasks"][0]["licence_spdx"] = "Apache-2.0"  # type: ignore[index]
    path = tmp_path / "intake.json"
    path.write_bytes(json.dumps(value).encode("utf-8"))
    with pytest.raises(CorpusError, match="not permitted"):
        load_public_source_intake(path)


def test_public_source_intake_rejects_mbpp_without_attribution_licence(
    tmp_path: Path,
) -> None:
    value = _function_manifest()
    value["tasks"][1]["licence_spdx"] = "MIT"  # type: ignore[index]
    path = tmp_path / "intake.json"
    path.write_bytes(json.dumps(value).encode("utf-8"))
    with pytest.raises(CorpusError, match="not permitted"):
        load_public_source_intake(path)


def test_public_source_intake_rejects_mixed_scope(tmp_path: Path) -> None:
    value = _function_manifest()
    value["tasks"][0]["source_kind"] = "swe_bench_verified"  # type: ignore[index]
    path = tmp_path / "intake.json"
    path.write_bytes(json.dumps(value).encode("utf-8"))
    with pytest.raises(CorpusError, match="mix"):
        load_public_source_intake(path)
