"""Deterministic public-practice compilation and local workflow helpers."""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile
from collections.abc import Iterable
from pathlib import Path
from typing import Any

from dittobench_coding_datagen.canonical import (
    canonical_json_bytes,
    safe_relative_path,
    sha256_hex,
    tree_identities,
)
from dittobench_coding_datagen.fixtures import fixture_for
from dittobench_coding_datagen.model import (
    CODING_CONTRACT_VERSION,
    PRACTICE_AGENT_INSTRUCTION,
    PRACTICE_SCHEMA,
    CorpusError,
    PracticeSource,
)
from dittobench_coding_datagen.validation import load_practice_source, validate_pack


def _write_bytes(root: Path, relative: str, body: bytes) -> None:
    relative = safe_relative_path(relative)
    destination = root / relative
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_bytes(body)


def _write_text(root: Path, relative: str, content: str) -> None:
    _write_bytes(root, relative, content.encode("utf-8"))


def _write_jsonl(root: Path, relative: str, records: Iterable[dict[str, Any]]) -> None:
    _write_bytes(
        root, relative, b"".join(canonical_json_bytes(record) for record in records)
    )


def _agent_user(user: dict[str, Any]) -> dict[str, Any]:
    return {
        "display_name": user["display_name"],
        "known_repositories": user["known_repositories"],
        "summary": user["summary"],
        "user_id": user["user_id"],
    }


def _agent_memory(memory: dict[str, Any]) -> dict[str, Any]:
    return {
        "content": memory["content"],
        "memory_id": memory["memory_id"],
        "owner_user_id": memory["owner_user_id"],
        "repository_id": memory.get("repository_id"),
        "supersedes": memory.get("supersedes", []),
        "valid_from_revision": memory.get("valid_from_revision"),
        "valid_until_revision": memory.get("valid_until_revision"),
    }


def _agent_task(task: dict[str, Any]) -> dict[str, Any]:
    task_id = task["task_id"]
    return {
        "active_user_id": task["active_user_id"],
        "base_revision": task["base_revision"],
        "instruction": PRACTICE_AGENT_INSTRUCTION,
        "problem_statement": task["problem_statement"],
        "repository_id": task["repository_id"],
        "task_id": task_id,
        "visible_capsule": f"capsules/{task_id}/visible",
    }


def _grader_task(
    task: dict[str, Any], grader_files: list[dict[str, Any]]
) -> dict[str, Any]:
    return {
        "base_revision": task["base_revision"],
        "fixture": task["fixture"],
        "grader_files": grader_files,
        "memory_condition": task["memory_condition"],
        "repository_id": task["repository_id"],
        "task_id": task["task_id"],
    }


def _compile_into(source: PracticeSource, root: Path) -> None:
    _write_jsonl(
        root, "agent/users.jsonl", (_agent_user(user) for user in source.users)
    )
    _write_jsonl(
        root,
        "agent/memories.jsonl",
        (_agent_memory(memory) for memory in source.memories),
    )
    _write_jsonl(
        root, "agent/tasks.jsonl", (_agent_task(task) for task in source.tasks)
    )

    grader_index: list[dict[str, Any]] = []
    for task in source.tasks:
        task_id = str(task["task_id"])
        fixture = fixture_for(str(task["fixture"]))
        visible_root = f"capsules/{task_id}/visible/workspace"
        for relative, content in sorted(fixture.base_files.items()):
            _write_text(root, f"{visible_root}/{safe_relative_path(relative)}", content)
        for relative, content in sorted(fixture.visible_tests.items()):
            _write_text(root, f"{visible_root}/{safe_relative_path(relative)}", content)
        grader_root = f"capsules/{task_id}/grader"
        for relative, content in sorted(fixture.grader_tests.items()):
            _write_text(root, f"{grader_root}/{safe_relative_path(relative)}", content)
        grader_files = [
            identity.as_json()
            for identity in tree_identities(root / "capsules" / task_id / "grader")
        ]
        grader_index.append(_grader_task(task, grader_files))
    _write_jsonl(root, "grader/tasks.jsonl", grader_index)

    source_body = source.source_path.read_bytes()
    identities = [identity.as_json() for identity in tree_identities(root)]
    manifest = {
        "coding_contract_version": CODING_CONTRACT_VERSION,
        "corpus_scope": "public_practice",
        "files": identities,
        "memory_count": len(source.memories),
        "practice_pack_id": source.pack_id,
        "schema": PRACTICE_SCHEMA,
        "source_sha256": sha256_hex(source_body),
        "task_count": len(source.tasks),
        "user_count": len(source.users),
        "weight_eligible": False,
    }
    _write_bytes(root, "manifest.json", canonical_json_bytes(manifest))


def compile_practice(
    source_path: Path, output: Path, *, replace: bool = False
) -> dict[str, Any]:
    """Compile source into an atomic canonical public practice pack."""

    source = load_practice_source(source_path)
    if output.is_symlink():
        raise CorpusError(f"output must not be a symlink: {output}")
    output = output.resolve()
    output.parent.mkdir(parents=True, exist_ok=True)
    if output.exists():
        if not replace:
            raise CorpusError(f"output already exists: {output}; pass --replace")
        validate_pack(output)
    with tempfile.TemporaryDirectory(
        prefix=f".{output.name}.", dir=output.parent
    ) as raw:
        staged = Path(raw) / "pack"
        staged.mkdir()
        _compile_into(source, staged)
        manifest = validate_pack(staged)
        if output.exists():
            shutil.rmtree(output)
        os.replace(staged, output)
    return manifest


def _task_index(pack: Path) -> dict[str, dict[str, Any]]:
    path = pack / "agent/tasks.jsonl"
    records = [
        json.loads(line)
        for line in path.read_text(encoding="utf-8").splitlines()
        if line
    ]
    return {str(record["task_id"]): record for record in records}


def materialize(pack: Path, task_id: str, output: Path) -> Path:
    validate_pack(pack)
    task = _task_index(pack).get(task_id)
    if task is None:
        raise CorpusError(f"unknown practice task: {task_id}")
    source = pack / str(task["visible_capsule"]) / "workspace"
    if output.exists():
        raise CorpusError(f"workspace output already exists: {output}")
    shutil.copytree(source, output, symlinks=False)
    return output


def grade(
    pack: Path, task_id: str, workspace: Path, *, timeout_seconds: int = 30
) -> int:
    """Run the public practice grader in a disposable copy of workspace."""

    validate_pack(pack)
    if task_id not in _task_index(pack):
        raise CorpusError(f"unknown practice task: {task_id}")
    if not workspace.is_dir() or workspace.is_symlink():
        raise CorpusError(f"workspace is not a real directory: {workspace}")
    for path in workspace.rglob("*"):
        relative = path.relative_to(workspace).as_posix()
        safe_relative_path(relative)
        if path.is_symlink():
            raise CorpusError(f"workspace contains a symlink: {relative}")
    with tempfile.TemporaryDirectory(prefix="dittobench-coding-grade-") as raw:
        grading = Path(raw) / "workspace"
        shutil.copytree(workspace, grading, symlinks=False)
        grader = pack / "capsules" / task_id / "grader"
        for path in sorted(grader.rglob("*")):
            if path.is_symlink():
                raise CorpusError("practice grader contains a symlink")
            if not path.is_file():
                continue
            relative = safe_relative_path(path.relative_to(grader).as_posix())
            destination = grading / relative
            destination.parent.mkdir(parents=True, exist_ok=True)
            destination.write_bytes(path.read_bytes())
        try:
            result = subprocess.run(
                [
                    sys.executable,
                    "-m",
                    "unittest",
                    "discover",
                    "-s",
                    "tests",
                    "-p",
                    "test_*.py",
                ],
                cwd=grading,
                check=False,
                timeout=timeout_seconds,
            )
        except subprocess.TimeoutExpired:
            return 124
        return result.returncode
