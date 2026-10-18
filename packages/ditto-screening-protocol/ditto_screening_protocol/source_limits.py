"""Per-file source limits shared by the miner CLI, Platform upload and screener.

A reviewer reads source 400 lines at a time, so a file far past these limits
is readable but not reviewable, and a file the screener cannot read leaves the
review dossier incomplete forever. Unmodified upstream releases are exempt by
exact SHA-256 (``data/vendored_upstream_allowlist.json``); any edit makes the
file first-party source again.

Measured on 2026-10-18 across ten v13 submissions (592 first-party files): p99
4,255 lines, largest 7,848 lines / 480 KB, longest line 2,856 characters.
"""

from __future__ import annotations

import codecs
import hashlib
import json
from collections.abc import Iterable, Iterator
from dataclasses import dataclass
from datetime import UTC, datetime
from functools import cache
from pathlib import Path, PurePosixPath
from typing import Literal, Protocol

MAX_SOURCE_FILE_LINES = 10_000
MAX_SOURCE_FILE_BYTES = 1024 * 1024
MAX_SOURCE_LINE_CHARS = 4_000

# Uploads before this instant are grandfathered; the CLI warns until then.
SOURCE_LIMITS_EFFECTIVE_AT = datetime(2026, 10, 25, tzinfo=UTC)

SOURCE_EXTENSIONS = frozenset(
    {
        ".c", ".cc", ".cpp", ".cxx", ".h", ".hh", ".hpp", ".hxx", ".inc",
        ".py", ".pyi", ".rs", ".go", ".zig",
        ".js", ".mjs", ".cjs", ".ts", ".tsx", ".jsx",
        ".java", ".kt", ".scala", ".rb", ".lua", ".php", ".pl", ".swift",
        ".cs", ".sh", ".bash",
    }
)  # fmt: skip

LimitKind = Literal["lines", "bytes", "line_length"]

_ALLOWLIST_PATH = Path(__file__).parent / "data" / "vendored_upstream_allowlist.json"


@dataclass(frozen=True)
class SourceLimitViolation:
    """One source file over a per-file limit."""

    path: str
    kind: LimitKind
    value: int
    limit: int

    @property
    def message(self) -> str:
        unit = {
            "lines": "lines",
            "bytes": "bytes",
            "line_length": "characters on one line",
        }[self.kind]
        return (
            f"{self.path}: {self.value:,} {unit} (limit {self.limit:,}). Split the "
            "file, or vendor an unmodified upstream release from the allowlist."
        )


@cache
def vendored_upstream_digests() -> frozenset[str]:
    """SHA-256 digests of allowlisted unmodified upstream release files."""
    document = json.loads(_ALLOWLIST_PATH.read_text(encoding="utf-8"))
    return frozenset(entry["sha256"] for entry in document["files"])


def is_source_path(path: str) -> bool:
    return PurePosixPath(path).suffix.lower() in SOURCE_EXTENSIONS


# Archive members are checked in chunks so one oversized member never has to be
# materialized; only the running digest, counters and decoder state are kept.
SOURCE_READ_CHUNK_BYTES = 64 * 1024


class _Reader(Protocol):
    def read(self, size: int = ..., /) -> bytes: ...


def read_chunks(reader: _Reader) -> Iterator[bytes]:
    """Yield ``reader`` in ``SOURCE_READ_CHUNK_BYTES`` pieces until EOF."""
    while chunk := reader.read(SOURCE_READ_CHUNK_BYTES):
        yield chunk


def check_source_stream(
    path: str, chunks: Iterable[bytes]
) -> SourceLimitViolation | None:
    """Return the first limit a streamed source member breaks, or ``None``.

    Non-source paths and non-UTF-8 content are out of scope (the archive and
    opaque-file contracts govern those); an allowlisted digest is exempt.
    Lines are counted like ``str.splitlines`` for ``\\n``-terminated text.
    """
    if not is_source_path(path):
        return None
    digest = hashlib.sha256()
    decoder = codecs.getincrementaldecoder("utf-8")()
    size = newlines = current = longest = 0
    try:
        for chunk in chunks:
            digest.update(chunk)
            size += len(chunk)
            text = decoder.decode(chunk)
            for segment in text.split("\n")[:-1]:
                longest = max(longest, current + len(segment))
                current = 0
                newlines += 1
            current += len(text) - (text.rfind("\n") + 1)
        tail = decoder.decode(b"", final=True)
    except UnicodeDecodeError:
        return None
    current += len(tail)
    longest = max(longest, current)
    lines = newlines + (1 if current else 0)
    violation: SourceLimitViolation | None = None
    if lines > MAX_SOURCE_FILE_LINES:
        violation = SourceLimitViolation(path, "lines", lines, MAX_SOURCE_FILE_LINES)
    elif size > MAX_SOURCE_FILE_BYTES:
        violation = SourceLimitViolation(path, "bytes", size, MAX_SOURCE_FILE_BYTES)
    elif longest > MAX_SOURCE_LINE_CHARS:
        violation = SourceLimitViolation(
            path, "line_length", longest, MAX_SOURCE_LINE_CHARS
        )
    if violation is not None and digest.hexdigest() in vendored_upstream_digests():
        return None
    return violation


def check_source_file(path: str, data: bytes) -> SourceLimitViolation | None:
    """``check_source_stream`` for an in-memory file."""
    return check_source_stream(path, (data,))


def source_limits_enforced(now: datetime) -> bool:
    return now >= SOURCE_LIMITS_EFFECTIVE_AT
