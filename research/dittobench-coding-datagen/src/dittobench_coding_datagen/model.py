"""Contract constants and typed internal records."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal

CODING_CONTRACT_VERSION = 1
PRACTICE_SCHEMA = "dittobench-coding-practice-v1"
PRACTICE_AGENT_INSTRUCTION = (
    "Resolve the issue in the supplied checkout. Use available user memory only "
    "when relevant; current instructions, code, and tests are authoritative."
)

type MemoryCondition = Literal[
    "required_constraint",
    "relevant_nonrequired",
    "irrelevant",
    "stale_conflicting",
    "current_override",
]

MEMORY_CONDITIONS: frozenset[str] = frozenset(
    {
        "required_constraint",
        "relevant_nonrequired",
        "irrelevant",
        "stale_conflicting",
        "current_override",
    }
)


class CorpusError(ValueError):
    """Raised when source or emitted corpus bytes violate the contract."""


type JsonValue = (
    None | bool | int | float | str | list["JsonValue"] | dict[str, "JsonValue"]
)


@dataclass(frozen=True)
class FileIdentity:
    """Content identity for one canonical pack file."""

    path: str
    size_bytes: int
    sha256: str

    def as_json(self) -> dict[str, JsonValue]:
        return {
            "path": self.path,
            "sha256": self.sha256,
            "size_bytes": self.size_bytes,
        }


@dataclass(frozen=True)
class PracticeSource:
    """Validated public practice source document."""

    pack_id: str
    users: tuple[dict[str, Any], ...]
    memories: tuple[dict[str, Any], ...]
    tasks: tuple[dict[str, Any], ...]
    source_path: Path
