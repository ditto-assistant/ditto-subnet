"""Aggregate complete external public-v2 task outcomes into one local report."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from dittobench_coding_datagen.canonical import canonical_json_bytes, sha256_hex
from dittobench_coding_datagen.local_result import (
    LocalPracticeTaskResult,
    build_local_practice_result,
)
from dittobench_coding_datagen.model import CorpusError
from dittobench_coding_datagen.public_pack_v2 import validate_public_v2_pack


def aggregate_public_v2_results(
    *, pack: Path, harness_artifact_sha256: str, task_result_paths: tuple[Path, ...]
) -> bytes:
    """Verify ten externally run task outcomes against a public v2 pack."""

    manifest = validate_public_v2_pack(pack)
    try:
        index = [
            json.loads(line)
            for line in (pack / "tasks" / "index.jsonl")
            .read_text(encoding="utf-8")
            .splitlines()
        ]
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as error:
        raise CorpusError("public v2 task index is unreadable") from error
    expected = {str(item["task_id"]): str(item["condition"]) for item in index}
    if len(task_result_paths) != len(expected):
        raise CorpusError("public practice requires exactly one result per task")
    tasks = tuple(
        _load_task_result(path, expected=expected) for path in task_result_paths
    )
    if {task.task_id for task in tasks} != set(expected):
        raise CorpusError("public practice result set does not match pack")
    result = build_local_practice_result(
        public_release_id=str(manifest["public_release_id"]),
        public_release_manifest_sha256=sha256_hex(
            (pack / "manifest.json").read_bytes()
        ),
        harness_artifact_sha256=harness_artifact_sha256,
        tasks=tasks,
    )
    return result.canonical_bytes()


def _load_task_result(
    path: Path, *, expected: dict[str, str]
) -> LocalPracticeTaskResult:
    if path.is_symlink() or not path.is_file() or path.stat().st_size > 1 << 20:
        raise CorpusError("public task result file is unsafe")
    try:
        body = path.read_bytes()
        raw: Any = json.loads(body)
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as error:
        raise CorpusError("public task result file is invalid") from error
    if not isinstance(raw, dict) or set(raw) != {
        "task_id",
        "condition",
        "resolved",
        "protocol_valid",
        "patch_valid",
        "terminal_domain",
    }:
        raise CorpusError("public task result fields are invalid")
    if canonical_json_bytes(raw) != body:
        raise CorpusError("public task result is not canonical JSON")
    task_id = raw["task_id"]
    condition = raw["condition"]
    if (
        not isinstance(task_id, str)
        or expected.get(task_id) != condition
        or type(raw["resolved"]) is not bool
        or type(raw["protocol_valid"]) is not bool
        or type(raw["patch_valid"]) is not bool
        or raw["terminal_domain"]
        not in {"resolved", "repair_failure", "harness_failure"}
        or (raw["terminal_domain"] == "resolved") != raw["resolved"]
    ):
        raise CorpusError("public task result authority is invalid")
    return LocalPracticeTaskResult(**raw)


__all__ = ["aggregate_public_v2_results"]
