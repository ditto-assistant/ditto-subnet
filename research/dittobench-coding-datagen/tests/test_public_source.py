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
