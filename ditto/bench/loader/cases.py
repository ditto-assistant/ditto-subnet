"""Fixture dataclasses + JSONL loaders.

The JSONL field names form the on-disk contract validators and miners
share. The Go validator binary defines its own equivalent of these
structs; both implementations are kept in lockstep over a single on-disk
fixture corpus by the parity tests in ``ditto/tests/bench/``.
"""

from __future__ import annotations

import json
from collections.abc import Iterator
from dataclasses import dataclass, field
from enum import StrEnum
from pathlib import Path
from typing import Any


class ArgMatcherKind(StrEnum):
    """Supported argument matcher kinds.

    String values are the on-disk contract. New kinds must be added
    consciously and bumped via ``ditto.bench.SCHEMA_VERSION``.
    """

    EXACT = "exact"
    CONTAINS = "contains"
    REGEX = "regex"
    URL_LIST = "url_list"
    MEMORY_ID_LIST = "memory_id_list"
    STRING_ARRAY_CONTAINS = "string_array_contains"
    FORBIDDEN = "forbidden"
    PRESENT = "present"


@dataclass(slots=True)
class ArgMatcher:
    """One semantic expectation on a single tool-call argument key.

    ``value`` is used by single-value kinds (exact/contains/regex). ``any_of``
    is used by multi-value kinds (url_list/memory_id_list/forbidden). ``weight``
    biases per-argument F1 toward important matchers; defaults to 1.0.
    """

    kind: ArgMatcherKind
    key: str
    value: str = ""
    any_of: list[str] = field(default_factory=list)
    weight: float = 0.0

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> ArgMatcher:
        """Parse one JSON object into an :class:`ArgMatcher`."""
        return cls(
            kind=ArgMatcherKind(d["kind"]),
            key=d.get("key", ""),
            value=d.get("value", ""),
            any_of=list(d.get("any_of", []) or []),
            weight=float(d.get("weight", 0.0) or 0.0),
        )


@dataclass(slots=True)
class ExpectedToolCall:
    """What a correct tool invocation looks like for one expected tool.

    ``required_args`` is the legacy exact-match map kept for backward
    compatibility; new fixtures should use ``arg_matchers``.
    """

    name: str
    required_args: dict[str, str] = field(default_factory=dict)
    arg_matchers: list[ArgMatcher] = field(default_factory=list)
    forbidden_args: list[str] = field(default_factory=list)
    forbidden_values: dict[str, list[str]] = field(default_factory=dict)

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> ExpectedToolCall:
        """Parse one JSON object into an :class:`ExpectedToolCall`."""
        return cls(
            name=d["name"],
            required_args=dict(d.get("required_args", {}) or {}),
            arg_matchers=[
                ArgMatcher.from_dict(m) for m in (d.get("arg_matchers") or [])
            ],
            forbidden_args=list(d.get("forbidden_args", []) or []),
            forbidden_values={
                k: list(v) for k, v in (d.get("forbidden_values") or {}).items()
            },
        )


@dataclass(slots=True)
class ToolCallCase:
    """A single DittoCore prompt + expected tool usage record."""

    id: str
    category: str
    prompt: str
    expected_tools: list[ExpectedToolCall] = field(default_factory=list)
    domain: str = ""
    max_tool_calls: int = 0
    allow_extra_tools: bool = False
    expected_behavior: str = ""
    visibility: str = ""
    tags: list[str] = field(default_factory=list)

    @property
    def is_public(self) -> bool:
        """True when this case ships in the public dev set."""
        return self.visibility in ("", "public")

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> ToolCallCase:
        """Parse one JSON object into a :class:`ToolCallCase`."""
        return cls(
            id=d["id"],
            category=d.get("category", ""),
            prompt=d.get("prompt", ""),
            expected_tools=[
                ExpectedToolCall.from_dict(t) for t in (d.get("expected_tools") or [])
            ],
            domain=d.get("domain", ""),
            max_tool_calls=int(d.get("max_tool_calls", 0) or 0),
            allow_extra_tools=bool(d.get("allow_extra_tools", False)),
            expected_behavior=d.get("expected_behavior", ""),
            visibility=d.get("visibility", ""),
            tags=list(d.get("tags", []) or []),
        )


@dataclass(slots=True)
class STMMessage:
    """One simulated conversation turn used for STM testing."""

    role: str
    content: str

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> STMMessage:
        """Parse one JSON object into an :class:`STMMessage`."""
        return cls(role=d.get("role", ""), content=d.get("content", ""))


@dataclass(slots=True)
class RetrievalCase:
    """A single DittoRetrieval test case against a seeded fixture user."""

    id: str
    category: str
    query: str
    user_fixture_id: str
    expected_pair_ids: list[str] = field(default_factory=list)
    forbidden_pair_ids: list[str] = field(default_factory=list)
    k: int = 0
    in_window: bool = False
    difficulty: str = ""
    visibility: str = ""
    tags: list[str] = field(default_factory=list)
    expected_content: str = ""
    stm_context: list[STMMessage] = field(default_factory=list)
    expected_answer: str = ""
    expect_no_tools: bool = False

    @property
    def is_public(self) -> bool:
        """True when this case ships in the public dev set."""
        return self.visibility in ("", "public")

    @property
    def is_stm(self) -> bool:
        """True when this case tests short-term-memory recall."""
        return len(self.stm_context) > 0

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> RetrievalCase:
        """Parse one JSON object into a :class:`RetrievalCase`."""
        return cls(
            id=d["id"],
            category=d.get("category", ""),
            query=d.get("query", ""),
            user_fixture_id=d.get("user_fixture_id", ""),
            expected_pair_ids=list(d.get("expected_pair_ids", []) or []),
            forbidden_pair_ids=list(d.get("forbidden_pair_ids", []) or []),
            k=int(d.get("k", 0) or 0),
            in_window=bool(d.get("in_window", False)),
            difficulty=d.get("difficulty", ""),
            visibility=d.get("visibility", ""),
            tags=list(d.get("tags", []) or []),
            expected_content=d.get("expected_content", ""),
            stm_context=[STMMessage.from_dict(m) for m in (d.get("stm_context") or [])],
            expected_answer=d.get("expected_answer", ""),
            expect_no_tools=bool(d.get("expect_no_tools", False)),
        )


class DuplicateCaseIDError(ValueError):
    """Raised when two fixture lines share the same ``id`` within a directory.

    The Go loader does NOT enforce this, but in Python we treat it as a hard
    error because the runner uses ``case.id`` as the report key. Sneaking in
    a duplicate would silently overwrite scores during aggregation.
    """


def _for_each_jsonl(
    directory: Path,
) -> Iterator[tuple[Path, int, dict[str, Any]]]:
    """Yield ``(path, line_num, record)`` for every ``*.jsonl`` line in ``directory``.

    Skips empty lines and ``//`` / ``#`` comments to match the Go loader.
    """
    for entry in sorted(directory.iterdir()):
        if entry.is_dir() or entry.suffix != ".jsonl":
            continue
        with entry.open("r", encoding="utf-8") as f:
            for line_num, raw in enumerate(f, start=1):
                line = raw.strip()
                if not line or line.startswith(("//", "#")):
                    continue
                try:
                    record = json.loads(line)
                except json.JSONDecodeError as e:
                    raise ValueError(f"{entry}:{line_num}: {e}") from e
                yield entry, line_num, record


def load_toolcall_cases(directory: Path | str) -> list[ToolCallCase]:
    """Load every ``*.jsonl`` under ``directory`` as :class:`ToolCallCase`.

    Raises :class:`DuplicateCaseIDError` if two cases share the same ``id``.
    """
    directory = Path(directory)
    out: list[ToolCallCase] = []
    seen: dict[str, Path] = {}
    for path, _line_num, record in _for_each_jsonl(directory):
        case = ToolCallCase.from_dict(record)
        if case.id in seen:
            raise DuplicateCaseIDError(
                f"duplicate toolcall id {case.id!r} in {path} "
                f"(first seen in {seen[case.id]})"
            )
        seen[case.id] = path
        out.append(case)
    return out


def load_retrieval_cases(directory: Path | str) -> list[RetrievalCase]:
    """Load every ``*.jsonl`` under ``directory`` as :class:`RetrievalCase`.

    Raises :class:`DuplicateCaseIDError` if two cases share the same ``id``.
    """
    directory = Path(directory)
    out: list[RetrievalCase] = []
    seen: dict[str, Path] = {}
    for path, _line_num, record in _for_each_jsonl(directory):
        case = RetrievalCase.from_dict(record)
        if case.id in seen:
            raise DuplicateCaseIDError(
                f"duplicate retrieval id {case.id!r} in {path} "
                f"(first seen in {seen[case.id]})"
            )
        seen[case.id] = path
        out.append(case)
    return out
