"""Validate external public-task staging without importing task bytes into Git."""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from pathlib import Path

from dittobench_coding_datagen.canonical import canonical_json_bytes, tree_identities
from dittobench_coding_datagen.model import CorpusError
from dittobench_coding_datagen.public_source import PublicSourceIntake

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
        task_root = root / "tasks" / source.task_id
        snapshot_manifest = _read_control(task_root / "snapshot" / "manifest.json")
        if _sha256(snapshot_manifest) != source.source_snapshot_manifest_sha256:
            raise CorpusError("public task snapshot manifest does not match intake")
        workspace = task_root / "snapshot" / "workspace"
        if workspace.is_symlink() or not workspace.is_dir():
            raise CorpusError("public task workspace is unsafe")
        workspace_tree = _identity_digest(workspace)
        grader = task_root / "grader"
        if grader.is_symlink() or not grader.is_dir():
            raise CorpusError("public task grader is unsafe")
        grader_sha256 = _identity_digest(grader)
        if grader_sha256 != source.visible_grader_sha256:
            raise CorpusError("public task grader does not match intake")
        staged.append(
            StagedPublicTask(
                task_id=source.task_id,
                workspace_tree_sha256=workspace_tree,
                visible_grader_sha256=grader_sha256,
                issue_sha256=_sha256(_read_control(task_root / "issue.json")),
                memory_sha256=_sha256(_read_control(task_root / "memory.json")),
                runtime_policy_sha256=_sha256(
                    _read_control(task_root / "runtime-policy.json")
                ),
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


def _identity_digest(root: Path) -> str:
    return _sha256(
        canonical_json_bytes([identity.as_json() for identity in tree_identities(root)])
    )


def _sha256(body: bytes) -> str:
    return hashlib.sha256(body).hexdigest()


__all__ = [
    "PUBLIC_STAGING_SCHEMA",
    "PublicTaskStaging",
    "StagedPublicTask",
    "validate_public_task_staging",
]
