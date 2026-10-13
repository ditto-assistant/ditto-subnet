"""Whitespace-normalized source windows used only as reviewer search leads."""

from __future__ import annotations

import hashlib
import json
import re
from typing import Annotated
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

MAX_ANCESTOR_WINDOWS = 32
MAX_WINDOW_TOKENS = 256
_MASK = (1 << 64) - 1
_BASE = 257
# Keep quoted contents intact: changing whitespace inside a literal is a code
# change, while changing indentation or spacing between tokens is not.
_TOKEN = re.compile(r'"(?:\\.|[^"\\])*"|\'(?:\\.|[^\'\\])*\'|\w+|[^\s]', re.UNICODE)


class RejectedAncestorWindow(BaseModel):
    """A cited window from an exact, rejected artifact; never policy proof."""

    model_config = ConfigDict(extra="ignore")
    agent_id: UUID
    artifact_sha256: Annotated[str, Field(pattern=r"^[0-9a-f]{64}$")]
    path: Annotated[str, Field(min_length=1, max_length=240)]
    start_line: Annotated[int, Field(ge=1)]
    end_line: Annotated[int, Field(ge=1)]
    token_count: Annotated[int, Field(ge=8, le=MAX_WINDOW_TOKENS)]
    sha256: Annotated[str, Field(pattern=r"^[0-9a-f]{64}$")]
    rolling_hash: Annotated[str, Field(pattern=r"^[0-9a-f]{16}$")]

    @field_validator("path")
    @classmethod
    def safe_path(cls, value: str) -> str:
        if (
            value.startswith("/")
            or "\\" in value
            or any(part in {"", ".", ".."} for part in value.split("/"))
        ):
            raise ValueError("window path must be a canonical relative path")
        return value

    @model_validator(mode="after")
    def valid_span(self) -> RejectedAncestorWindow:
        if self.end_line < self.start_line or self.end_line - self.start_line > 39:
            raise ValueError("window must cover between one and forty lines")
        return self


def source_tokens(text: str) -> list[tuple[str, int]]:
    """Return tokens and their one-based line numbers without changing literals."""
    result: list[tuple[str, int]] = []
    line, end = 1, 0
    for match in _TOKEN.finditer(text):
        line += text.count("\n", end, match.start())
        result.append((match.group(), line))
        line += match.group().count("\n")
        end = match.end()
    return result


def window_sha256(tokens: list[str]) -> str:
    body = json.dumps(tokens, ensure_ascii=False, separators=(",", ":")).encode()
    return hashlib.sha256(b"ditto/rejected-ancestor-window/v1\0" + body).hexdigest()


def _token_number(token: str) -> int:
    return int.from_bytes(hashlib.sha256(token.encode()).digest()[:8], "big")


def rolling_hash(tokens: list[str]) -> str:
    value = 0
    for token in tokens:
        value = (value * _BASE + _token_number(token)) & _MASK
    return f"{value:016x}"


def matching_windows(
    text: str, windows: list[RejectedAncestorWindow], *, limit: int = 16
) -> list[tuple[int, RejectedAncestorWindow]]:
    """Match moved/reformatted code, verifying every rolling-hash hit with SHA.

    The rolling hash only avoids hashing each full token window. Its collisions
    cannot create a match, and neither kind of match certifies a violation.
    """
    if limit <= 0 or not windows:
        return []
    tokens = source_tokens(text)
    values = [_token_number(token) for token, _line in tokens]
    by_size: dict[int, dict[int, list[RejectedAncestorWindow]]] = {}
    for window in windows[:MAX_ANCESTOR_WINDOWS]:
        by_size.setdefault(window.token_count, {}).setdefault(
            int(window.rolling_hash, 16), []
        ).append(window)
    matches: list[tuple[int, RejectedAncestorWindow]] = []
    for size, targets in sorted(by_size.items()):
        if size > len(tokens):
            continue
        value = 0
        for number in values[:size]:
            value = (value * _BASE + number) & _MASK
        factor = pow(_BASE, size - 1, 1 << 64)
        for start in range(len(tokens) - size + 1):
            if start:
                value = (
                    (value - values[start - 1] * factor) * _BASE
                    + values[start + size - 1]
                ) & _MASK
            if value in targets:
                digest = window_sha256(
                    [token for token, _line in tokens[start : start + size]]
                )
                for target in targets[value]:
                    if digest == target.sha256:
                        matches.append((tokens[start][1], target))
                        if len(matches) >= limit:
                            return matches
    return matches
