"""Validated public-task intake records; repository bytes stay outside Git."""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Literal
from urllib.parse import urlparse

from dittobench_coding_datagen.canonical import canonical_json_bytes, safe_opaque_id
from dittobench_coding_datagen.model import CorpusError

PublicLanguage = Literal["python", "typescript", "rust", "go"]
PublicCondition = Literal[
    "v0_none",
    "v1_relevant",
    "v2_irrelevant",
    "v3_stale_conflict",
    "v4_current_override",
]

_LANGUAGE_COUNTS: dict[PublicLanguage, int] = {
    "python": 3,
    "typescript": 3,
    "rust": 2,
    "go": 2,
}
_CONDITIONS: tuple[PublicCondition, ...] = (
    "v0_none",
    "v1_relevant",
    "v2_irrelevant",
    "v3_stale_conflict",
    "v4_current_override",
)

# Registry of reputable open coding datasets accepted as public problem sources.
# Every kind is an OSI/CC open licence so the shadow router pack can be
# redistributed. ``licence_spdx`` on a task must match the allow-list for its
# kind exactly; this is the load-bearing guard against relicensed or
# unattributed intake. SWE-bench (Verified/Lite/Multilingual) and HumanEval are
# MIT; MBPP ships under CC-BY-4.0. ``public_maintainer`` remains the escape hatch
# for hand-authored public tasks and accepts a small permissive licence set.
PublicSourceKind = Literal[
    "swe_bench_verified",
    "swe_bench_lite",
    "swe_bench_multilingual",
    "humaneval",
    "mbpp",
    "public_maintainer",
]
_SOURCE_KIND_LICENCES: dict[str, frozenset[str]] = {
    "swe_bench_verified": frozenset({"MIT"}),
    "swe_bench_lite": frozenset({"MIT"}),
    "swe_bench_multilingual": frozenset({"MIT"}),
    "humaneval": frozenset({"MIT"}),
    "mbpp": frozenset({"CC-BY-4.0"}),
    "public_maintainer": frozenset(
        {"MIT", "Apache-2.0", "BSD-3-Clause", "BSD-2-Clause", "CC-BY-4.0", "ISC"}
    ),
}
# Repository-scoped kinds carry a whole-repo snapshot and honour the SWE-bench
# release profile (four repository families, the 3/3/2/2 language split).
# Function-scoped kinds (HumanEval, MBPP) are single-function Python problems and
# use a relaxed, Python-only release profile.
_REPOSITORY_SCOPED_KINDS: frozenset[str] = frozenset(
    {
        "swe_bench_verified",
        "swe_bench_lite",
        "swe_bench_multilingual",
        "public_maintainer",
    }
)
_FUNCTION_SCOPED_KINDS: frozenset[str] = frozenset({"humaneval", "mbpp"})


@dataclass(frozen=True)
class PublicTaskSource:
    task_id: str
    repository_family: str
    language: PublicLanguage
    licence_spdx: str
    source_kind: PublicSourceKind
    public_issue_url: str
    source_snapshot_manifest_sha256: str
    source_snapshot_archive_sha256: str
    visible_grader_sha256: str
    condition: PublicCondition

    def as_json(self) -> dict[str, object]:
        return {
            "condition": self.condition,
            "language": self.language,
            "licence_spdx": self.licence_spdx,
            "public_issue_url": self.public_issue_url,
            "repository_family": self.repository_family,
            "source_kind": self.source_kind,
            "source_snapshot_archive_sha256": self.source_snapshot_archive_sha256,
            "source_snapshot_manifest_sha256": self.source_snapshot_manifest_sha256,
            "task_id": self.task_id,
            "visible_grader_sha256": self.visible_grader_sha256,
        }


@dataclass(frozen=True)
class PublicSourceIntake:
    schema: str
    public_release_id: str
    tasks: tuple[PublicTaskSource, ...]

    def canonical_bytes(self) -> bytes:
        return canonical_json_bytes(
            {
                "public_release_id": self.public_release_id,
                "schema": self.schema,
                "tasks": [task.as_json() for task in self.tasks],
            }
        )


def load_public_source_intake(path: Path) -> PublicSourceIntake:
    """Load one public-only, ten-task intake manifest without fetching sources."""

    if path.is_symlink() or not path.is_file() or path.stat().st_size > 1 << 20:
        raise CorpusError("public source intake file is unsafe")
    try:
        raw = json.loads(path.read_bytes())
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as error:
        raise CorpusError("public source intake is not valid JSON") from error
    if not isinstance(raw, dict) or set(raw) != {
        "schema",
        "public_release_id",
        "tasks",
    }:
        raise CorpusError("public source intake fields are invalid")
    if raw["schema"] != "dittobench-coding-public-source-intake-v2":
        raise CorpusError("public source intake schema is invalid")
    try:
        public_release_id = safe_opaque_id(raw["public_release_id"])
    except CorpusError as error:
        raise CorpusError("public source intake release identity is invalid") from error
    if not isinstance(raw["tasks"], list):
        raise CorpusError("public source intake tasks are invalid")
    tasks = tuple(_parse_task(task) for task in raw["tasks"])
    _validate_release(tasks)
    return PublicSourceIntake(
        schema="dittobench-coding-public-source-intake-v2",
        public_release_id=public_release_id,
        tasks=tasks,
    )


def _parse_task(raw: object) -> PublicTaskSource:
    if not isinstance(raw, dict) or set(raw) != {
        "task_id",
        "repository_family",
        "language",
        "licence_spdx",
        "source_kind",
        "public_issue_url",
        "source_snapshot_manifest_sha256",
        "source_snapshot_archive_sha256",
        "visible_grader_sha256",
        "condition",
    }:
        raise CorpusError("public source task fields are invalid")
    values = {name: raw[name] for name in raw}
    try:
        values["task_id"] = safe_opaque_id(values["task_id"])
        values["repository_family"] = safe_opaque_id(values["repository_family"])
        values["licence_spdx"] = safe_opaque_id(values["licence_spdx"])
    except CorpusError as error:
        raise CorpusError("public source task authority is invalid") from error
    if (
        values["language"] not in _LANGUAGE_COUNTS
        or values["source_kind"] not in _SOURCE_KIND_LICENCES
        or values["condition"] not in _CONDITIONS
        or not _public_https_url(values["public_issue_url"])
        or any(
            not _sha256(values[field])
            for field in (
                "source_snapshot_manifest_sha256",
                "source_snapshot_archive_sha256",
                "visible_grader_sha256",
            )
        )
    ):
        raise CorpusError("public source task authority is invalid")
    if values["licence_spdx"] not in _SOURCE_KIND_LICENCES[values["source_kind"]]:
        raise CorpusError(
            f"licence {values['licence_spdx']!r} is not permitted for source kind "
            f"{values['source_kind']!r}"
        )
    return PublicTaskSource(**values)


def _validate_release(tasks: tuple[PublicTaskSource, ...]) -> None:
    if len(tasks) != 10 or len({task.task_id for task in tasks}) != 10:
        raise CorpusError("public source intake must contain ten unique tasks")
    kinds = {task.source_kind for task in tasks}
    repository_scoped = kinds <= _REPOSITORY_SCOPED_KINDS
    function_scoped = kinds <= _FUNCTION_SCOPED_KINDS
    if repository_scoped == function_scoped:
        raise CorpusError(
            "public source intake must be uniformly repository-scoped or "
            "function-scoped, not a mix"
        )
    for condition in _CONDITIONS:
        if sum(task.condition == condition for task in tasks) != 2:
            raise CorpusError("public source intake condition split is invalid")
    if function_scoped:
        _validate_function_release(tasks)
    else:
        _validate_repository_release(tasks)


def _validate_repository_release(tasks: tuple[PublicTaskSource, ...]) -> None:
    if len({task.repository_family for task in tasks}) != 4:
        raise CorpusError("public source intake must contain four repository families")
    for language, expected in _LANGUAGE_COUNTS.items():
        if sum(task.language == language for task in tasks) != expected:
            raise CorpusError("public source intake language split is invalid")


def _validate_function_release(tasks: tuple[PublicTaskSource, ...]) -> None:
    # HumanEval and MBPP are single-function Python problems, so the multi-repo,
    # multi-language SWE-bench split does not apply; the memory-condition split
    # still holds because this remains a memory benchmark.
    if any(task.language != "python" for task in tasks):
        raise CorpusError("function-scoped public intake must be all Python")


def _sha256(value: object) -> bool:
    return (
        isinstance(value, str)
        and len(value) == 64
        and all(character in "0123456789abcdef" for character in value)
    )


def _public_https_url(value: object) -> bool:
    if not isinstance(value, str) or len(value) > 4096:
        return False
    parsed = urlparse(value)
    return (
        parsed.scheme == "https"
        and parsed.hostname == "github.com"
        and parsed.username is None
        and parsed.password is None
        and not parsed.fragment
    )


__all__ = [
    "PublicSourceIntake",
    "PublicSourceKind",
    "PublicTaskSource",
    "load_public_source_intake",
]
