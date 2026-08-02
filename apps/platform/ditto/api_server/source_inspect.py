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

import io
import tarfile
from dataclasses import dataclass

MAX_LISTING_FILES = 512
MAX_OPAQUE_BLOBS = 128
MAX_READ_LINES = 400
TEXT_SIZE_LIMIT = 2 * 1024 * 1024
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
    "MAX_TARBALL_BYTES",
    "MAX_UNPACKED_BYTES",
    "SourceInspectError",
    "TarSourceInspector",
]
