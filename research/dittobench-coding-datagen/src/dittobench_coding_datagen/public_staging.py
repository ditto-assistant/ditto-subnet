"""Validate external public-task staging without importing task bytes into Git."""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from pathlib import Path

from dittobench_coding_datagen.canonical import safe_opaque_id
from dittobench_coding_datagen.model import CorpusError
from dittobench_coding_datagen.public_controls import validate_public_task_controls
from dittobench_coding_datagen.public_source import PublicSourceIntake
from dittobench_coding_datagen.snapshot import validate_sanitized_snapshot
from dittobench_coding_datagen.snapshot_archive import verify_snapshot_archive

PUBLIC_STAGING_SCHEMA = "dittobench-coding-public-task-staging-v2"
_MAX_CONTROL_FILE_BYTES = 1 << 20


@dataclass(frozen=True)
class StagedPublicTask:
    task_id: str
    workspace_tree_sha256: str
    visible_grader_sha256: str
    issue_sha256: str
    memory_sha256: str
    runtime_policy_sha256: str

    def as_json(self) -> dict[str, object]:
        return {
            "issue_sha256": self.issue_sha256,
            "memory_sha256": self.memory_sha256,
            "runtime_policy_sha256": self.runtime_policy_sha256,
            "task_id": self.task_id,
            "visible_grader_sha256": self.visible_grader_sha256,
            "workspace_tree_sha256": self.workspace_tree_sha256,
        }


@dataclass(frozen=True)
class PublicTaskStaging:
    schema: str
    public_release_id: str
    tasks: tuple[StagedPublicTask, ...]

    def as_json(self) -> dict[str, object]:
        return {
            "public_release_id": self.public_release_id,
            "schema": self.schema,
            "tasks": [task.as_json() for task in self.tasks],
        }


def validate_public_task_staging(
    *, root: Path, intake: PublicSourceIntake
) -> PublicTaskStaging:
    """Verify that every externally staged task matches its reviewed intake."""

    if root.is_symlink() or not root.is_dir():
        raise CorpusError("public task staging root is unsafe")
    staged: list[StagedPublicTask] = []
    for source in intake.tasks:
        task_id = safe_opaque_id(source.task_id)
        task_root = root / "tasks" / task_id
        snapshot_manifest = _read_control(task_root / "snapshot" / "manifest.json")
        if _sha256(snapshot_manifest) != source.source_snapshot_manifest_sha256:
            raise CorpusError("public task snapshot manifest does not match intake")
        snapshot = validate_sanitized_snapshot(task_root / "snapshot")
        workspace_tree = snapshot.snapshot_tree_sha256
        archive = task_root / "snapshot" / "archive.tar.gz"
        if archive.is_symlink() or not archive.is_file():
            raise CorpusError("public task snapshot archive is unavailable")
        archive_receipt = verify_snapshot_archive(archive)
        if (
            archive_receipt.archive_sha256 != source.source_snapshot_archive_sha256
            or archive_receipt.snapshot_manifest_sha256
            != source.source_snapshot_manifest_sha256
            or archive_receipt.snapshot_tree_sha256 != workspace_tree
        ):
            raise CorpusError("public task snapshot archive does not match intake")
        controls = validate_public_task_controls(
            task_root=task_root,
            task_id=task_id,
            condition=source.condition,
            workspace=task_root / "snapshot" / "workspace",
        )
        if controls.visible_grader_sha256 != source.visible_grader_sha256:
            raise CorpusError("public task grader does not match intake")
        staged.append(
            StagedPublicTask(
                task_id=task_id,
                workspace_tree_sha256=workspace_tree,
                visible_grader_sha256=controls.visible_grader_sha256,
                issue_sha256=controls.issue_sha256,
                memory_sha256=controls.memory_sha256,
                runtime_policy_sha256=controls.runtime_policy_sha256,
            )
        )
    return PublicTaskStaging(
        schema=PUBLIC_STAGING_SCHEMA,
        public_release_id=intake.public_release_id,
        tasks=tuple(staged),
    )


def _read_control(path: Path) -> bytes:
    if (
        path.is_symlink()
        or not path.is_file()
        or path.stat().st_size > _MAX_CONTROL_FILE_BYTES
    ):
        raise CorpusError("public task control file is unsafe")
    try:
        body = path.read_bytes()
    except OSError as error:
        raise CorpusError("public task control file is unreadable") from error
    if not body:
        raise CorpusError("public task control file is empty")
    return body


def _sha256(body: bytes) -> str:
    return hashlib.sha256(body).hexdigest()


__all__ = [
    "PUBLIC_STAGING_SCHEMA",
    "PublicTaskStaging",
    "StagedPublicTask",
    "validate_public_task_staging",
]
