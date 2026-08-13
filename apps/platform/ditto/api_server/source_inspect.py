"""Bounded, read-only inspection of an uploaded submission tarball.

Serves the operator quarantine-review console: the screener's source-review
finding flags ``path:line`` evidence, and these helpers let an authenticated
operator read exactly those bounded excerpts server-side without downloading
and unpacking the artifact locally.

The tarball is UNTRUSTED miner input, so the reader mirrors the screener's
defensive posture (:mod:`ditto_screener.source_review`) and is explicitly
DoS-bounded: member count and total declared unpacked size are capped, the
archive is characterized in ONE sequential decompression pass at construction
(no per-file rescans), reads are line- and size-bounded, and non-UTF-8 or
oversized files are reported as opaque blobs with explicit totals instead of
being silently invisible or silently truncated.

Construction and reads are synchronous CPU work; endpoint callers run them via
``asyncio.to_thread`` so archive parsing never blocks the event loop.
"""

from __future__ import annotations

import fnmatch
import io
import re
import tarfile
from dataclasses import dataclass

MAX_LISTING_FILES = 512
MAX_OPAQUE_BLOBS = 128
MAX_READ_LINES = 400
TEXT_SIZE_LIMIT = 2 * 1024 * 1024
# Search bounds. A miner ``baseline.rs`` routinely runs 10k+ lines, so the scan
# has to cross whole crates while the *response* stays small enough for an MCP
# client's token ceiling: at most MAX_SEARCH_MATCHES rows per page, each line
# clipped to SEARCH_LINE_CHARS, and at most MAX_SEARCH_CONTEXT lines on either
# side. MAX_SEARCH_SCAN bounds the work itself so a pattern matching every line
# of a hostile archive cannot pin a worker thread building a list it will
# discard.
MAX_SEARCH_MATCHES = 200
MAX_SEARCH_CONTEXT = 5
MAX_SEARCH_SCAN = 5000
MAX_SEARCH_PATTERN_CHARS = 200
SEARCH_LINE_CHARS = 500
# Uploads are capped well below these (20 MiB compressed by default); the
# bounds only protect the API process from a mis-sized or hostile object.
MAX_TARBALL_BYTES = 64 * 1024 * 1024
MAX_MEMBERS = 4096
MAX_UNPACKED_BYTES = 256 * 1024 * 1024


class SourceInspectError(Exception):
    """Raised when the archive or a requested member cannot be inspected."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


@dataclass(frozen=True)
class _Member:
    name: str
    archive_name: str
    size: int
    is_text: bool


def _safe_name(member: tarfile.TarInfo) -> str | None:
    """Normalized member path, or ``None`` for unsafe/non-regular entries."""
    normalized = member.name.removeprefix("./")
    parts = normalized.split("/")
    if (
        not normalized
        or normalized.startswith("/")
        or ".." in parts
        or "\\" in normalized
        or not member.isfile()
    ):
        return None
    return normalized


def _compile_search(pattern: str, mode: str, ignore_case: bool) -> re.Pattern[str]:
    """Compile an operator search term, failing as a typed inspect error.

    The pattern is operator input, not miner input, but a bad regex must still
    come back as a 422 with the engine's own message rather than a 500.
    """
    if not pattern or len(pattern) > MAX_SEARCH_PATTERN_CHARS:
        raise SourceInspectError(
            "search-pattern-invalid",
            f"pattern must be 1-{MAX_SEARCH_PATTERN_CHARS} characters",
        )
    if mode not in ("regex", "literal"):
        raise SourceInspectError("search-mode-invalid", f"unknown search mode {mode!r}")
    expression = re.escape(pattern) if mode == "literal" else pattern
    try:
        return re.compile(expression, re.IGNORECASE if ignore_case else 0)
    except re.error as error:
        raise SourceInspectError(
            "search-pattern-invalid", f"pattern is not a valid regex: {error}"
        ) from error


def _path_selected(path: str, path_glob: str | None) -> bool:
    """Match a normalized member path against an optional shell glob.

    ``*`` crosses ``/`` (so ``src/*.rs`` reaches nested modules), and a glob
    with no separator is also tried against the basename, so ``*.rs`` and
    ``baseline.rs`` both do what an operator means by them.
    """
    if path_glob is None:
        return True
    if fnmatch.fnmatchcase(path, path_glob):
        return True
    return "/" not in path_glob and fnmatch.fnmatchcase(
        path.rsplit("/", 1)[-1], path_glob
    )


class TarSourceInspector:
    """A read-only, size-bounded view over regular files in a tarball."""

    def __init__(self, tar_bytes: bytes) -> None:
        self._tar_bytes = tar_bytes
        members: list[_Member] = []
        count = 0
        unpacked = 0
        try:
            # Stream mode: exactly one sequential decompression pass both
            # inventories the archive and determines UTF-8 readability, so a
            # hostile archive cannot force repeated full rescans.
            with tarfile.open(fileobj=io.BytesIO(tar_bytes), mode="r|gz") as archive:
                for member in archive:
                    count += 1
                    if count > MAX_MEMBERS:
                        raise SourceInspectError(
                            "artifact-too-many-members",
                            f"archive exceeds {MAX_MEMBERS} members",
                        )
                    unpacked += max(0, member.size)
                    if unpacked > MAX_UNPACKED_BYTES:
                        raise SourceInspectError(
                            "artifact-too-large",
                            f"archive exceeds {MAX_UNPACKED_BYTES} unpacked bytes",
                        )
                    normalized = _safe_name(member)
                    if normalized is None:
                        continue
                    members.append(
                        _Member(
                            normalized,
                            member.name,
                            member.size,
                            self._member_is_text(archive, member),
                        )
                    )
        except SourceInspectError:
            raise
        except (tarfile.TarError, OSError, EOFError) as error:
            raise SourceInspectError(
                "artifact-unreadable", f"artifact is not a readable tarball: {error}"
            ) from error
        self._members = {member.name: member for member in members}

    @staticmethod
    def _member_is_text(archive: tarfile.TarFile, member: tarfile.TarInfo) -> bool:
        if member.size > TEXT_SIZE_LIMIT:
            return False
        extracted = archive.extractfile(member)
        if extracted is None:
            return False
        raw = extracted.read(TEXT_SIZE_LIMIT + 1)
        try:
            raw.decode("utf-8")
        except UnicodeDecodeError:
            return False
        return True

    def listing(self) -> dict[str, object]:
        """Bounded inventory with explicit totals for every truncation."""
        ordered = sorted(self._members.values(), key=lambda item: item.name)
        rows = [
            {"path": item.name, "bytes": item.size}
            for item in ordered[:MAX_LISTING_FILES]
        ]
        opaque_all = [item for item in ordered if not item.is_text]
        return {
            "file_count": len(self._members),
            "files": rows,
            "opaque_blobs": [
                {
                    "path": item.name,
                    "bytes": item.size,
                    "reason": "oversized"
                    if item.size > TEXT_SIZE_LIMIT
                    else "non_utf8",
                }
                for item in opaque_all[:MAX_OPAQUE_BLOBS]
            ],
            "opaque_total": len(opaque_all),
            "truncated": len(ordered) > len(rows),
        }

    def read_all_text(
        self, *, max_files: int = MAX_LISTING_FILES, max_total_bytes: int | None = None
    ) -> dict[str, str]:
        """Extract every UTF-8 text member's full content in ONE pass.

        Feeds pair diffing, which needs whole-file bodies for both artifacts at
        once; doing that with per-file :meth:`read` calls would reopen the gzip
        stream once per file. The result is bounded: at most ``max_files``
        members (smallest first, so a hostile archive of many tiny files can't
        crowd out the real sources) and, when set, ``max_total_bytes`` of
        cumulative decoded text. Members past either bound are simply omitted —
        the diff manifest reports the archive's true ``file_count`` separately
        so the omission is visible, never silent.
        """
        budget = TEXT_SIZE_LIMIT if max_total_bytes is None else max_total_bytes
        wanted = {
            member.archive_name: member
            for member in sorted(
                (m for m in self._members.values() if m.is_text),
                key=lambda item: (item.size, item.name),
            )[:max_files]
        }
        out: dict[str, str] = {}
        if not wanted:
            return out
        total = 0
        with tarfile.open(fileobj=io.BytesIO(self._tar_bytes), mode="r|gz") as archive:
            for member in archive:
                info = wanted.get(member.name)
                if info is None:
                    continue
                extracted = archive.extractfile(member)
                if extracted is None:
                    continue
                text = extracted.read(TEXT_SIZE_LIMIT + 1).decode("utf-8")
                if total + len(text) > budget:
                    continue
                total += len(text)
                out[info.name] = text
                if len(out) == len(wanted):
                    break
        return out

    def search(
        self,
        pattern: str,
        *,
        mode: str = "regex",
        ignore_case: bool = False,
        path_glob: str | None = None,
        context: int = 0,
        limit: int = 50,
        offset: int = 0,
    ) -> dict[str, object]:
        """Find ``pattern`` across every readable member in ONE archive pass.

        Deciding a source review means locating where the agent builds the
        graded response — a construction that sits at line 8919 of 10795 as
        often as line 12. Bisecting to it with 400-line
        :meth:`read` calls costs six to eight reads per artifact; this answers
        the same question in one, and the operator then reads only the region
        that matters.

        Opaque members (non-UTF-8, or past :data:`TEXT_SIZE_LIMIT`) are never
        searched — they are the same blobs :meth:`listing` reports separately —
        and their count is returned so the omission is explicit rather than
        silent. Every bound that clipped the answer is likewise reported:
        ``truncated`` means the scan stopped at :data:`MAX_SEARCH_SCAN` matches
        and the totals are lower bounds, and ``has_more`` means this page ends
        before the matches the scan did find do.
        """
        matcher = _compile_search(pattern, mode, ignore_case)
        wanted = {
            member.archive_name: member
            for member in self._members.values()
            if member.is_text and _path_selected(member.name, path_glob)
        }
        context = max(0, min(context, MAX_SEARCH_CONTEXT))
        limit = max(1, min(limit, MAX_SEARCH_MATCHES))
        offset = max(0, offset)
        hits: list[tuple[str, int, list[str]]] = []
        files_searched = 0
        files_matched = 0
        truncated = False
        if wanted:
            with tarfile.open(
                fileobj=io.BytesIO(self._tar_bytes), mode="r|gz"
            ) as archive:
                for member in archive:
                    info = wanted.get(member.name)
                    if info is None:
                        continue
                    extracted = archive.extractfile(member)
                    if extracted is None:
                        continue
                    files_searched += 1
                    lines = (
                        extracted.read(TEXT_SIZE_LIMIT + 1).decode("utf-8").splitlines()
                    )
                    matched_here = False
                    for index, line in enumerate(lines, start=1):
                        if not matcher.search(line):
                            continue
                        matched_here = True
                        if len(hits) >= MAX_SEARCH_SCAN:
                            truncated = True
                            break
                        hits.append((info.name, index, lines))
                    if matched_here:
                        files_matched += 1
                    if truncated or files_searched == len(wanted):
                        break
        # Archive order is arbitrary; sort so the same query answers the same
        # way twice and an operator can page it without rows shifting under them.
        hits.sort(key=lambda hit: (hit[0], hit[1]))
        window = hits[offset : offset + limit]
        return {
            "pattern": pattern,
            "mode": mode,
            "path_glob": path_glob,
            "matches": [
                {
                    "path": path,
                    "line": line,
                    "text": lines[line - 1][:SEARCH_LINE_CHARS],
                    "context_before": [
                        {"line": number, "text": lines[number - 1][:SEARCH_LINE_CHARS]}
                        for number in range(max(1, line - context), line)
                    ],
                    "context_after": [
                        {"line": number, "text": lines[number - 1][:SEARCH_LINE_CHARS]}
                        for number in range(
                            line + 1, min(len(lines), line + context) + 1
                        )
                    ],
                }
                for path, line, lines in window
            ],
            "match_count": len(hits),
            "returned": len(window),
            "limit": limit,
            "offset": offset,
            "has_more": offset + len(window) < len(hits),
            "files_searched": files_searched,
            "files_matched": files_matched,
            "opaque_skipped": sum(
                1 for item in self._members.values() if not item.is_text
            ),
            "truncated": truncated,
        }

    def read(self, path: str, start_line: int, end_line: int) -> dict[str, object]:
        """Read a bounded line range from one UTF-8 text member."""
        normalized = path.removeprefix("./")
        member = self._members.get(normalized)
        if member is None:
            raise SourceInspectError("file-not-found", f"no file at {normalized!r}")
        if not member.is_text:
            raise SourceInspectError(
                "file-is-not-utf8-text",
                f"{normalized!r} is binary or exceeds the {TEXT_SIZE_LIMIT} byte "
                "text bound",
            )
        text = self._read_text(member)
        lines = text.splitlines()
        start = max(1, start_line)
        end = max(start, min(end_line, start + MAX_READ_LINES - 1, len(lines)))
        return {
            "path": normalized,
            "total_lines": len(lines),
            "start_line": start,
            "end_line": min(end, len(lines)),
            "lines": [
                {"line": index, "text": lines[index - 1][:500]}
                for index in range(start, min(end, len(lines)) + 1)
            ],
        }

    def _read_text(self, member_info: _Member) -> str:
        # One targeted decompression per excerpt request; the constructor
        # already proved the member is bounded UTF-8 text.
        with tarfile.open(fileobj=io.BytesIO(self._tar_bytes), mode="r:gz") as archive:
            member = archive.getmember(member_info.archive_name)
            extracted = archive.extractfile(member)
            if extracted is None:
                raise SourceInspectError(
                    "file-not-found", f"no file at {member_info.name!r}"
                )
            return extracted.read(TEXT_SIZE_LIMIT + 1).decode("utf-8")


__all__ = [
    "MAX_LISTING_FILES",
    "MAX_MEMBERS",
    "MAX_READ_LINES",
    "MAX_SEARCH_CONTEXT",
    "MAX_SEARCH_MATCHES",
    "MAX_SEARCH_PATTERN_CHARS",
    "MAX_SEARCH_SCAN",
    "MAX_TARBALL_BYTES",
    "MAX_UNPACKED_BYTES",
    "SEARCH_LINE_CHARS",
    "SourceInspectError",
    "TarSourceInspector",
]
