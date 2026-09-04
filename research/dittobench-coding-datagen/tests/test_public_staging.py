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
from dittobench_coding_datagen.public_controls import (
    PUBLIC_GRADER_SCHEMA,
    PUBLIC_ISSUE_SCHEMA,
    PUBLIC_MEMORY_SCHEMA,
    PUBLIC_RUNTIME_SCHEMA,
)
from dittobench_coding_datagen.public_source import load_public_source_intake
from dittobench_coding_datagen.public_staging import validate_public_task_staging
from dittobench_coding_datagen.snapshot import SNAPSHOT_SCHEMA
from dittobench_coding_datagen.snapshot_archive import build_snapshot_archive


def _sha(body: bytes) -> str:
    return hashlib.sha256(body).hexdigest()


def _command(command_id: str) -> dict[str, object]:
    return {
        "argv": ["pytest", "-q"],
        "environment": {},
        "id": command_id,
        "timeout_milliseconds": 30_000,
    }


def _write_task(root: Path, task_id: str, condition: str) -> tuple[str, str, str]:
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
    grader_files = grader / "files"
    grader_files.mkdir(parents=True)
    grader_file = grader_files / "test_public.py"
    grader_file.write_text(task_id, encoding="utf-8")
    grader_file.chmod(0o755)
    grader_identity = normalized_tree_identities(grader)[0]
    grader_manifest = {
        "build_command": None,
        "environment_image_digest": f"sha256:{'a' * 64}",
        "environment_platform": "linux/amd64",
        "files": [
            {
                "destination_path": "tests/test_public.py",
                "mode": grader_identity["mode"],
                "sha256": grader_identity["sha256"],
                "size_bytes": grader_identity["size_bytes"],
                "source_path": grader_identity["path"],
            }
        ],
        "schema": PUBLIC_GRADER_SCHEMA,
        "task_id": task_id,
        "test_groups": [
            {
                "command": _command("grader-fail-to-pass"),
                "expected_tests": ["test-fail-to-pass"],
                "group": "fail_to_pass",
            },
            {
                "command": _command("grader-pass-to-pass"),
                "expected_tests": ["test-pass-to-pass"],
                "group": "pass_to_pass",
            },
        ],
    }
    (grader / "manifest.json").write_bytes(canonical_json_bytes(grader_manifest))
    (grader / "manifest.json").chmod(0o644)
    issue = {
        "constraints": ["Do not add a runtime dependency."],
        "description": f"Repair the public behavior for {task_id}.",
        "schema": PUBLIC_ISSUE_SCHEMA,
        "task_id": task_id,
        "title": f"Repair {task_id}",
    }
    memories = []
    if condition != "v0_none":
        memories = [
            {
                "confidence_micros": 900_000,
                "content": f"Historical context for {task_id}.",
                "fact_group_id": f"fact-{task_id}",
                "memory_id": f"memory-{task_id}",
                "repository_capability_id": f"repository-{task_id}",
                "scope": "repository",
                "supersedes": [],
                "type": "project_fact",
                "valid_from_epoch": "epoch-1",
                "valid_until_epoch": None,
            }
        ]
    memory = {
        "condition": condition,
        "memories": memories,
        "schema": PUBLIC_MEMORY_SCHEMA,
        "task_id": task_id,
    }
    runtime_policy = {
        "build_commands": [],
        "creatable_paths": [],
        "deletable_paths": [],
        "editable_paths": ["app.txt"],
        "environment_image_digest": f"sha256:{'a' * 64}",
        "environment_platform": "linux/amd64",
        "limits": {
            "cpu_quota_millis": 1_000,
            "max_patch_bytes": 1 << 20,
            "max_workspace_bytes": 1 << 20,
            "memory_limit_bytes": 512 << 20,
            "pids_limit": 256,
            "wall_time_seconds": 120,
        },
        "network": "none",
        "schema": PUBLIC_RUNTIME_SCHEMA,
        "task_id": task_id,
        "test_commands": [_command("visible-tests")],
    }
    (task / "issue.json").write_bytes(canonical_json_bytes(issue))
    (task / "memory.json").write_bytes(canonical_json_bytes(memory))
    (task / "runtime-policy.json").write_bytes(canonical_json_bytes(runtime_policy))
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
        manifest_sha, archive_sha, grader_sha = _write_task(root, task_id, condition)
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
    with pytest.raises(CorpusError, match="grader files"):
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
    (task / "grader" / "files" / "test_public.py").chmod(0o644)
    with pytest.raises(CorpusError, match="grader file identity"):
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
