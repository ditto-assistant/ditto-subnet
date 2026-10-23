"""Upload-time checks for generator source archives.

Python port of ``challenge.InspectArchive`` and ``challenge.TreeDigest`` in
``research/dittobench-datagen/challenge/archive.go``. Platform runs it on every
upload so an archive the scorer would refuse never enters the queue, and so
``tree_sha256`` (the content identity that ignores tar order, timestamps,
permissions, and compression) can refuse re-packed copies of an earlier
upload. ``ditto/tests/test_generator_archive.py`` replays the Go archive vector.

This never extracts anything to disk and never executes anything: it reads tar
members into memory under hard size and count bounds.
"""

from __future__ import annotations

import gzip
import hashlib
import io
import posixpath
import re
import tarfile
import zlib
from dataclasses import dataclass

MAX_ARCHIVE_BYTES = 4 << 20
MAX_SOURCE_BYTES = 32 << 20
MAX_SOURCE_FILES = 4096
MAX_SOURCE_PATH_LENGTH = 240
BUILDER_GO_VERSION = "1.27.3"
# The tar stream around MAX_SOURCE_BYTES of file payload: a 512-byte header
# and up to 511 bytes of padding per entry (two headers for a long path), plus
# the end markers and record padding. Bounding decompression by this keeps a
# legal archive at the file-payload limit acceptable.
MAX_TAR_BYTES = MAX_SOURCE_BYTES + (MAX_SOURCE_FILES + 2) * 2048 + (20 << 10)

_MODULE_LINE = re.compile(r'^module\s+("?)([^\s"]+)("?)\s*(//.*)?$')
_GO_LINE = re.compile(r"^go\s+([0-9]+\.[0-9]+(\.[0-9]+)?)\s*(//.*)?$")
_REQUIRE = re.compile(r"^require(\s|\()")


class ArchiveRejected(ValueError):
    """The archive cannot be a v1 generator source tree."""


@dataclass(frozen=True)
class ArchiveInfo:
    sha256: str
    tree_sha256: str
    module: str
    go_version: str
    files: int
    bytes: int
    vendored: bool


def tree_digest(files: dict[str, bytes]) -> str:
    """SHA-256 over ``path NUL hex(sha256(content)) LF`` in path order."""
    h = hashlib.sha256()
    for path in sorted(files):
        h.update(path.encode())
        h.update(b"\0")
        h.update(hashlib.sha256(files[path]).hexdigest().encode())
        h.update(b"\n")
    return h.hexdigest()


def _clean_path(name: str) -> str:
    trimmed = name
    if trimmed.startswith("./"):
        trimmed = trimmed[2:]
    trimmed = trimmed.removesuffix("/")
    if trimmed in ("", "."):
        raise ArchiveRejected(f"source archive entry {name!r} has an empty path")
    if len(trimmed) > MAX_SOURCE_PATH_LENGTH:
        raise ArchiveRejected(
            f"source archive entry {name!r} path is longer than "
            f"{MAX_SOURCE_PATH_LENGTH}"
        )
    if "\\" in trimmed or "\0" in trimmed or trimmed.startswith("/"):
        raise ArchiveRejected(
            f"source archive entry {name!r} path must be relative with forward slashes"
        )
    if any(part in ("..", ".") for part in trimmed.split("/")):
        raise ArchiveRejected(f"source archive entry {name!r} path escapes the root")
    if posixpath.normpath(trimmed) != trimmed:
        raise ArchiveRejected(f"source archive entry {name!r} path is not clean")
    return trimmed


def _compare_go(a: str, b: str) -> int:
    pa = [int(x) for x in a.split(".")] + [0, 0, 0]
    pb = [int(x) for x in b.split(".")] + [0, 0, 0]
    return (pa[:3] > pb[:3]) - (pa[:3] < pb[:3])


def _parse_go_mod(raw: bytes) -> tuple[str, str, bool]:
    module = go_version = ""
    requires = False
    for line in raw.decode("utf-8", errors="replace").splitlines():
        line = line.strip()
        if m := _MODULE_LINE.match(line):
            module = m.group(2)
        elif m := _GO_LINE.match(line):
            go_version = m.group(1)
        elif _REQUIRE.match(line):
            requires = True
    return module, go_version, requires


def inspect_archive(raw: bytes) -> ArchiveInfo:
    """Check a gzip-compressed tar of generator source. Raises ArchiveRejected."""
    if len(raw) > MAX_ARCHIVE_BYTES:
        raise ArchiveRejected(f"source archive exceeds {MAX_ARCHIVE_BYTES} bytes")
    try:
        # Bound decompression before tarfile sees anything: a gzip bomb fails
        # here, not in memory.
        decompressor = zlib.decompressobj(16 + zlib.MAX_WBITS)
        tar_bytes = decompressor.decompress(raw, MAX_TAR_BYTES)
        if decompressor.unconsumed_tail:
            raise ArchiveRejected(
                f"source archive expands past {MAX_SOURCE_BYTES} bytes"
            )
    except (zlib.error, gzip.BadGzipFile) as exc:
        raise ArchiveRejected(f"source archive is not gzip: {exc}") from exc
    # Exactly one complete gzip member, as the Go check requires: a missing or
    # corrupt trailer leaves the stream unfinished, and anything after the
    # member (a second member, or trailing bytes) would be bytes the scorer
    # and this check could read differently.
    if not decompressor.eof:
        raise ArchiveRejected("source archive gzip stream is incomplete")
    if decompressor.unused_data:
        raise ArchiveRejected("source archive has data after its gzip member")
    files: dict[str, bytes] = {}
    total = 0
    entries = 0
    try:
        with tarfile.open(fileobj=io.BytesIO(tar_bytes), mode="r:") as tar:
            for member in tar:
                if member.type in (tarfile.XGLTYPE, tarfile.XHDTYPE):
                    continue
                entries += 1
                # Every entry counts; the root directory entry may be one extra.
                if entries > MAX_SOURCE_FILES + 1:
                    raise ArchiveRejected(
                        f"source archive has more than {MAX_SOURCE_FILES} entries"
                    )
                if not member.isreg() and member.size:
                    raise ArchiveRejected(
                        f"source archive entry {member.name!r} carries a payload "
                        "but is not a regular file"
                    )
                if member.isdir() and member.name in ("", ".", "./"):
                    continue  # the root itself, as `tar -C dir .` writes it
                name = _clean_path(member.name)
                if member.isdir():
                    continue
                if not member.isreg():
                    raise ArchiveRejected(
                        f"source archive entry {name!r} is not a regular file or "
                        "directory"
                    )
                if name in files:
                    raise ArchiveRejected(f"source archive repeats {name!r}")
                if member.size < 0 or total + member.size > MAX_SOURCE_BYTES:
                    raise ArchiveRejected(
                        f"source archive expands past {MAX_SOURCE_BYTES} bytes"
                    )
                handle = tar.extractfile(member)
                data = handle.read() if handle is not None else b""
                if len(data) != member.size:
                    raise ArchiveRejected(f"source archive entry {name!r} is truncated")
                total += member.size
                files[name] = data
    except tarfile.TarError as exc:
        raise ArchiveRejected(f"source archive: {exc}") from exc

    gomod = files.get("go.mod")
    if gomod is None:
        raise ArchiveRejected("source archive must have go.mod at its root")
    if not any("/" not in path and path.endswith(".go") for path in files):
        raise ArchiveRejected(
            "source archive must have the main package's .go files at its root"
        )
    module, go_version, requires = _parse_go_mod(gomod)
    if not module:
        raise ArchiveRejected("go.mod must declare a module path")
    if not go_version:
        raise ArchiveRejected("go.mod must declare a go version")
    if _compare_go(go_version, BUILDER_GO_VERSION) > 0:
        raise ArchiveRejected(
            f"go.mod requires go {go_version}; the builder runs go {BUILDER_GO_VERSION}"
        )
    vendored = "vendor/modules.txt" in files
    if requires and not vendored:
        raise ArchiveRejected(
            "go.mod has requirements but vendor/modules.txt is missing; run "
            "`go mod vendor` (the build has no network)"
        )
    return ArchiveInfo(
        sha256=hashlib.sha256(raw).hexdigest(),
        tree_sha256=tree_digest(files),
        module=module,
        go_version=go_version,
        files=len(files),
        bytes=total,
        vendored=vendored,
    )
