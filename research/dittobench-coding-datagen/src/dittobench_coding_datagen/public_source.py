"""Validated public-task intake records; repository bytes stay outside Git."""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Literal
from urllib.parse import urlparse

from dittobench_coding_datagen.canonical import canonical_json_bytes
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


@dataclass(frozen=True)
class PublicTaskSource:
    task_id: str
    repository_family: str
    language: PublicLanguage
    licence_spdx: str
    source_kind: Literal[
        "swe_bench_verified", "swe_bench_multilingual", "public_maintainer"
    ]
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
    if not _identifier(raw["public_release_id"]):
        raise CorpusError("public source intake release identity is invalid")
    if not isinstance(raw["tasks"], list):
        raise CorpusError("public source intake tasks are invalid")
    tasks = tuple(_parse_task(task) for task in raw["tasks"])
    _validate_release(tasks)
    return PublicSourceIntake(
        schema="dittobench-coding-public-source-intake-v2",
        public_release_id=raw["public_release_id"],
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
    if (
        not _identifier(values["task_id"])
        or not _identifier(values["repository_family"])
        or not _identifier(values["licence_spdx"])
        or values["language"] not in _LANGUAGE_COUNTS
        or values["source_kind"]
        not in {"swe_bench_verified", "swe_bench_multilingual", "public_maintainer"}
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
    return PublicTaskSource(**values)


def _validate_release(tasks: tuple[PublicTaskSource, ...]) -> None:
    if len(tasks) != 10 or len({task.task_id for task in tasks}) != 10:
        raise CorpusError("public source intake must contain ten unique tasks")
    if len({task.repository_family for task in tasks}) != 4:
        raise CorpusError("public source intake must contain four repository families")
    for language, expected in _LANGUAGE_COUNTS.items():
        if sum(task.language == language for task in tasks) != expected:
            raise CorpusError("public source intake language split is invalid")
    for condition in _CONDITIONS:
        if sum(task.condition == condition for task in tasks) != 2:
            raise CorpusError("public source intake condition split is invalid")


def _identifier(value: object) -> bool:
    return (
        isinstance(value, str)
        and bool(value)
        and len(value.encode("utf-8")) <= 256
        and not any(character.isspace() or ord(character) < 32 for character in value)
    )


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


__all__ = ["PublicSourceIntake", "PublicTaskSource", "load_public_source_intake"]
