from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest

from dittobench_coding_datagen.canonical import (
    canonical_json_bytes,
    normalized_tree_identities,
    normalized_tree_sha256,
)
from dittobench_coding_datagen.model import CorpusError
from dittobench_coding_datagen.public_source import load_public_source_intake
from dittobench_coding_datagen.public_staging import validate_public_task_staging
from dittobench_coding_datagen.snapshot import SNAPSHOT_SCHEMA
from dittobench_coding_datagen.snapshot_archive import build_snapshot_archive


def _sha(body: bytes) -> str:
    return hashlib.sha256(body).hexdigest()


def _write_task(root: Path, task_id: str) -> tuple[str, str, str]:
    task = root / "tasks" / task_id
    workspace = task / "snapshot" / "workspace"
    workspace.mkdir(parents=True)
    app = workspace / "app.txt"
    app.write_text(task_id, encoding="utf-8")
    app.chmod(0o755)
    identities = normalized_tree_identities(workspace)
    workspace_tree = normalized_tree_sha256(workspace)
    snapshot_manifest = canonical_json_bytes(
        {
            "excluded_root_entries": [],
            "files": identities,
            "schema": SNAPSHOT_SCHEMA,
            "snapshot_tree_sha256": workspace_tree,
            "source_tree_sha256": workspace_tree,
        }
    )
    (task / "snapshot" / "manifest.json").write_bytes(snapshot_manifest)
    (task / "snapshot" / "manifest.json").chmod(0o644)
    grader = task / "grader"
    grader.mkdir()
    grader_file = grader / "test.txt"
    grader_file.write_text(task_id, encoding="utf-8")
    grader_file.chmod(0o755)
    (task / "issue.json").write_bytes(canonical_json_bytes({"issue": task_id}))
    (task / "memory.json").write_bytes(canonical_json_bytes({"memories": []}))
    (task / "runtime-policy.json").write_bytes(canonical_json_bytes({"commands": []}))
    grader_digest = normalized_tree_sha256(grader)
    archive = task / "snapshot" / "archive.tar.gz"
    archive_receipt = build_snapshot_archive(
        snapshot=task / "snapshot", archive=archive
    )
    return _sha(snapshot_manifest), archive_receipt.archive_sha256, grader_digest


def _intake(root: Path) -> Path:
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
    tasks: list[dict[str, object]] = []
    for index, (language, condition) in enumerate(layout):
        task_id = f"PUBLIC-V2-{index:02d}"
        manifest_sha, archive_sha, grader_sha = _write_task(root, task_id)
        tasks.append(
            {
                "task_id": task_id,
                "repository_family": f"{language}-family",
                "language": language,
                "licence_spdx": "MIT",
                "source_kind": "swe_bench_verified"
                if language == "python"
                else "swe_bench_multilingual",
                "public_issue_url": f"https://github.com/example/{language}/issues/{index}",
                "source_snapshot_manifest_sha256": manifest_sha,
                "source_snapshot_archive_sha256": archive_sha,
                "visible_grader_sha256": grader_sha,
                "condition": condition,
            }
        )
    path = root / "intake.json"
    path.write_bytes(
        json.dumps(
            {
                "schema": "dittobench-coding-public-source-intake-v2",
                "public_release_id": "coding-public-v2",
                "tasks": tasks,
            }
        ).encode("utf-8")
    )
    return path


def test_staging_binds_every_external_task_to_intake(tmp_path: Path) -> None:
    intake = load_public_source_intake(_intake(tmp_path))
    staging = validate_public_task_staging(root=tmp_path, intake=intake)

    assert len(staging.tasks) == 10
    assert staging.public_release_id == "coding-public-v2"


def test_staging_rejects_grader_drift(tmp_path: Path) -> None:
    intake = load_public_source_intake(_intake(tmp_path))
    extra = tmp_path / "tasks" / "PUBLIC-V2-00" / "grader" / "extra.txt"
    extra.write_text("drift", encoding="utf-8")
    extra.chmod(0o644)
    with pytest.raises(CorpusError, match="grader does not match"):
        validate_public_task_staging(root=tmp_path, intake=intake)


def test_staging_rejects_workspace_drift_from_snapshot_manifest(tmp_path: Path) -> None:
    intake = load_public_source_intake(_intake(tmp_path))
    extra = tmp_path / "tasks" / "PUBLIC-V2-00" / "snapshot" / "workspace" / "extra.txt"
    extra.write_text("drift", encoding="utf-8")
    extra.chmod(0o644)
    with pytest.raises(CorpusError, match="snapshot does not match workspace"):
        validate_public_task_staging(root=tmp_path, intake=intake)


def test_staging_rejects_workspace_and_grader_mode_drift(tmp_path: Path) -> None:
    intake = load_public_source_intake(_intake(tmp_path))
    task = tmp_path / "tasks" / "PUBLIC-V2-00"
    (task / "snapshot" / "workspace" / "app.txt").chmod(0o644)
    with pytest.raises(CorpusError, match="snapshot does not match workspace"):
        validate_public_task_staging(root=tmp_path, intake=intake)

    (task / "snapshot" / "workspace" / "app.txt").chmod(0o755)
    (task / "grader" / "test.txt").chmod(0o644)
    with pytest.raises(CorpusError, match="grader does not match"):
        validate_public_task_staging(root=tmp_path, intake=intake)


def test_staging_requires_canonical_snapshot_archive(tmp_path: Path) -> None:
    intake = load_public_source_intake(_intake(tmp_path))
    archive = tmp_path / "tasks" / "PUBLIC-V2-00" / "snapshot" / "archive.tar.gz"
    archive.unlink()
    with pytest.raises(CorpusError, match="archive is unavailable"):
        validate_public_task_staging(root=tmp_path, intake=intake)

    intake = load_public_source_intake(_intake(tmp_path / "second"))
    archive = (
        tmp_path / "second" / "tasks" / "PUBLIC-V2-00" / "snapshot" / "archive.tar.gz"
    )
    archive.write_bytes(archive.read_bytes() + b"drift")
    with pytest.raises(CorpusError, match="canonical"):
        validate_public_task_staging(root=tmp_path / "second", intake=intake)
