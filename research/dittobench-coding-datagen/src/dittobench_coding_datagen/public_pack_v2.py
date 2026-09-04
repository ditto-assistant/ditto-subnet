"""Compile external public task staging into a deterministic v2 practice pack."""

from __future__ import annotations

import json
import os
import shutil
import tempfile
from pathlib import Path
from typing import Any

from dittobench_coding_datagen.canonical import (
    canonical_json_bytes,
    safe_relative_path,
    sha256_hex,
    tree_identities,
)
from dittobench_coding_datagen.model import CorpusError
from dittobench_coding_datagen.public_source import load_public_source_intake
from dittobench_coding_datagen.public_staging import validate_public_task_staging

PUBLIC_PACK_V2_SCHEMA = "dittobench-coding-public-practice-v2"


def compile_public_v2_pack(
    *, staging_root: Path, intake_path: Path, output: Path, replace: bool = False
) -> dict[str, Any]:
    """Compile one public-only ten-task pack from verified external staging."""

    intake = load_public_source_intake(intake_path)
    staging = validate_public_task_staging(root=staging_root, intake=intake)
    if output.is_symlink() or output.resolve().is_relative_to(staging_root.resolve()):
        raise CorpusError("public v2 pack output is unsafe")
    if output.exists():
        if not replace:
            raise CorpusError("public v2 pack output already exists")
        if not output.is_dir() or output.is_symlink():
            raise CorpusError("public v2 pack output is unsafe")
        shutil.rmtree(output)
    output.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(
        prefix=f".{output.name}.", dir=output.parent
    ) as raw:
        staged = Path(raw) / "pack"
        staged.mkdir()
        index: list[dict[str, object]] = []
        by_task = {task.task_id: task for task in staging.tasks}
        for source in intake.tasks:
            task_root = staging_root / "tasks" / source.task_id
            source_workspace = task_root / "snapshot" / "workspace"
            source_grader = task_root / "grader"
            _copy_tree(
                source_workspace,
                staged / "capsules" / source.task_id / "visible" / "workspace",
            )
            _copy_tree(source_grader, staged / "capsules" / source.task_id / "grader")
            issue = _copy_control(
                task_root / "issue.json",
                staged / "tasks" / source.task_id / "issue.json",
            )
            memory = _copy_control(
                task_root / "memory.json",
                staged / "tasks" / source.task_id / "memory.json",
            )
            policy = _copy_control(
                task_root / "runtime-policy.json",
                staged / "tasks" / source.task_id / "runtime-policy.json",
            )
            observed = by_task[source.task_id]
            index.append(
                {
                    "condition": source.condition,
                    "issue_sha256": sha256_hex(issue),
                    "language": source.language,
                    "memory_sha256": sha256_hex(memory),
                    "repository_family": source.repository_family,
                    "runtime_policy_sha256": sha256_hex(policy),
                    "task_id": source.task_id,
                    "visible_grader_sha256": observed.visible_grader_sha256,
                    "workspace_tree_sha256": observed.workspace_tree_sha256,
                }
            )
        _write_jsonl(staged / "tasks" / "index.jsonl", index)
        manifest = {
            "coding_contract_version": 2,
            "corpus_scope": "public_practice",
            "files": [item.as_json() for item in tree_identities(staged)],
            "public_release_id": intake.public_release_id,
            "schema": PUBLIC_PACK_V2_SCHEMA,
            "source_intake_sha256": sha256_hex(intake.canonical_bytes()),
            "staging_sha256": sha256_hex(canonical_json_bytes(staging.as_json())),
            "task_count": len(index),
            "weight_eligible": False,
        }
        (staged / "manifest.json").write_bytes(canonical_json_bytes(manifest))
        os.replace(staged, output)
    return validate_public_v2_pack(output)


def validate_public_v2_pack(pack: Path) -> dict[str, Any]:
    """Verify one public v2 pack's canonical manifest and ten task entries."""

    if pack.is_symlink() or not pack.is_dir():
        raise CorpusError("public v2 pack is unsafe")
    try:
        body = (pack / "manifest.json").read_bytes()
        manifest = json.loads(body)
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as error:
        raise CorpusError("public v2 pack manifest is invalid") from error
    if (
        not isinstance(manifest, dict)
        or manifest.get("schema") != PUBLIC_PACK_V2_SCHEMA
        or manifest.get("corpus_scope") != "public_practice"
        or manifest.get("coding_contract_version") != 2
        or manifest.get("weight_eligible") is not False
        or manifest.get("task_count") != 10
    ):
        raise CorpusError("public v2 pack manifest authority is invalid")
    without_manifest = [
        item.as_json()
        for item in tree_identities(pack, exclude=frozenset({"manifest.json"}))
    ]
    if manifest.get("files") != without_manifest:
        raise CorpusError("public v2 pack manifest file identities drifted")
    index = _read_jsonl(pack / "tasks" / "index.jsonl")
    if len(index) != 10:
        raise CorpusError("public v2 pack task index is invalid")
    task_ids: set[str] = set()
    for item in index:
        if not isinstance(item, dict) or not isinstance(item.get("task_id"), str):
            raise CorpusError("public v2 pack task index is invalid")
        task_id = item["task_id"]
        task_ids.add(task_id)
        for relative in (
            f"tasks/{task_id}/issue.json",
            f"tasks/{task_id}/memory.json",
            f"tasks/{task_id}/runtime-policy.json",
            f"capsules/{task_id}/visible/workspace",
            f"capsules/{task_id}/grader",
        ):
            if not (pack / relative).exists():
                raise CorpusError("public v2 pack task material is missing")
    if len(task_ids) != 10:
        raise CorpusError("public v2 pack task index is invalid")
    return manifest


def _copy_tree(source: Path, destination: Path) -> None:
    if source.is_symlink() or not source.is_dir():
        raise CorpusError("public v2 source tree is unsafe")
    for identity in tree_identities(source):
        relative = safe_relative_path(identity.path)
        original = source / relative
        target = destination / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(original, target, follow_symlinks=False)
        target.chmod(0o755 if original.stat().st_mode & 0o100 else 0o644)


def _copy_control(source: Path, destination: Path) -> bytes:
    if source.is_symlink() or not source.is_file():
        raise CorpusError("public v2 task control source is unsafe")
    body = source.read_bytes()
    try:
        parsed = json.loads(body)
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise CorpusError(
            "public v2 task control source is not canonical JSON"
        ) from error
    if canonical_json_bytes(parsed) != body:
        raise CorpusError("public v2 task control source is not canonical JSON")
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_bytes(body)
    destination.chmod(0o644)
    return body


def _write_jsonl(path: Path, records: list[dict[str, object]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"".join(canonical_json_bytes(record) for record in records))
    path.chmod(0o644)


def _read_jsonl(path: Path) -> list[object]:
    if path.is_symlink() or not path.is_file():
        raise CorpusError("public v2 pack task index is unsafe")
    try:
        return [
            json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()
        ]
    except json.JSONDecodeError as error:
        raise CorpusError("public v2 pack task index is invalid") from error


__all__ = ["PUBLIC_PACK_V2_SCHEMA", "compile_public_v2_pack", "validate_public_v2_pack"]
