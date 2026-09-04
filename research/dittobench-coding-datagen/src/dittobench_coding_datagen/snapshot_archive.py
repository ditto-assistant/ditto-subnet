"""Deterministic, mode-preserving archives for sanitized public snapshots."""

from __future__ import annotations

import gzip
import hashlib
import os
import tarfile
import tempfile
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Any

from dittobench_coding_datagen.canonical import safe_relative_path
from dittobench_coding_datagen.model import CorpusError
from dittobench_coding_datagen.snapshot import validate_sanitized_snapshot

SNAPSHOT_ARCHIVE_SCHEMA = "dittobench-coding-snapshot-archive-v1"
_ARCHIVE_ROOT = "dittobench-snapshot-v2"
_MAX_ARCHIVE_BYTES = 2 << 30
_MAX_UNCOMPRESSED_BYTES = 4 << 30
_MAX_MEMBERS = 100_000


@dataclass(frozen=True)
class SnapshotArchiveReceipt:
    schema: str
    archive_sha256: str
    archive_size_bytes: int
    snapshot_manifest_sha256: str
    snapshot_tree_sha256: str

    def as_json(self) -> dict[str, object]:
        return {
            "archive_sha256": self.archive_sha256,
            "archive_size_bytes": self.archive_size_bytes,
            "schema": self.schema,
            "snapshot_manifest_sha256": self.snapshot_manifest_sha256,
            "snapshot_tree_sha256": self.snapshot_tree_sha256,
        }


def build_snapshot_archive(
    *, snapshot: Path, archive: Path, replace: bool = False
) -> SnapshotArchiveReceipt:
    """Build one deterministic archive from a validated sanitized snapshot."""

    authority = validate_sanitized_snapshot(snapshot)
    resolved_archive = archive.resolve()
    resolved_snapshot = snapshot.resolve()
    if (
        archive.is_symlink()
        or resolved_archive == resolved_snapshot
        or resolved_archive == (resolved_snapshot / "manifest.json")
        or resolved_archive.is_relative_to(resolved_snapshot / "workspace")
        or (archive.exists() and (not replace or not archive.is_file()))
    ):
        raise CorpusError("snapshot archive output is unsafe")
    archive.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(
        prefix=f".{archive.name}.", dir=archive.parent
    ) as raw:
        staged = Path(raw) / archive.name
        _write_archive(snapshot, staged, authority.snapshot_tree_sha256)
        if staged.stat().st_size > _MAX_ARCHIVE_BYTES:
            raise CorpusError("snapshot archive exceeds its byte bound")
        staged.chmod(0o644)
        os.replace(staged, archive)
    receipt = verify_snapshot_archive(archive)
    if receipt.snapshot_tree_sha256 != authority.snapshot_tree_sha256:
        raise CorpusError("snapshot archive authority drifted")
    return receipt


def verify_snapshot_archive(archive: Path) -> SnapshotArchiveReceipt:
    """Verify metadata, bytes, modes, and the embedded snapshot authority."""

    if (
        archive.is_symlink()
        or not archive.is_file()
        or archive.stat().st_size < 1
        or archive.stat().st_size > _MAX_ARCHIVE_BYTES
    ):
        raise CorpusError("snapshot archive is unsafe")
    archive_size = archive.stat().st_size
    archive_sha256 = _file_sha256(archive)
    with tempfile.TemporaryDirectory(prefix="dittobench-snapshot-verify-") as raw:
        destination = Path(raw)
        tree_sha256, seen = _extract_archive(archive, destination)
        snapshot = destination / _ARCHIVE_ROOT / tree_sha256
        authority = validate_sanitized_snapshot(snapshot)
        expected = {"manifest.json"}
        expected.update(f"workspace/{item['path']}" for item in authority.files)
        if seen != expected or authority.snapshot_tree_sha256 != tree_sha256:
            raise CorpusError("snapshot archive members do not match manifest")
        manifest_sha256 = _file_sha256(snapshot / "manifest.json")
        canonical_archive = destination / "canonical.tar.gz"
        _write_archive(snapshot, canonical_archive, tree_sha256)
        if (
            canonical_archive.stat().st_size != archive_size
            or _file_sha256(canonical_archive) != archive_sha256
        ):
            raise CorpusError("snapshot archive bytes are not canonical")
    return SnapshotArchiveReceipt(
        schema=SNAPSHOT_ARCHIVE_SCHEMA,
        archive_sha256=archive_sha256,
        archive_size_bytes=archive_size,
        snapshot_manifest_sha256=manifest_sha256,
        snapshot_tree_sha256=tree_sha256,
    )


def _write_archive(snapshot: Path, archive: Path, tree_sha256: str) -> None:
    authority = validate_sanitized_snapshot(snapshot)
    members: list[tuple[str, Path, int, int]] = [
        (
            "manifest.json",
            snapshot / "manifest.json",
            (snapshot / "manifest.json").stat().st_size,
            0o644,
        )
    ]
    members.extend(
        (
            f"workspace/{safe_relative_path(str(item['path']))}",
            snapshot / "workspace" / str(item["path"]),
            int(item["size_bytes"]),
            int(item["mode"]),
        )
        for item in authority.files
    )
    with (
        archive.open("xb") as raw,
        gzip.GzipFile(
            fileobj=raw, mode="wb", filename="", mtime=0, compresslevel=9
        ) as zipped,
        tarfile.open(
            fileobj=zipped,
            mode="w|",
            format=tarfile.PAX_FORMAT,
            encoding="utf-8",
            errors="strict",
        ) as output,
    ):
        for relative, source, size, mode in members:
            info = tarfile.TarInfo(f"{_ARCHIVE_ROOT}/{tree_sha256}/{relative}")
            info.size = size
            info.mode = mode
            info.mtime = 0
            info.uid = 0
            info.gid = 0
            info.uname = ""
            info.gname = ""
            with source.open("rb") as body:
                output.addfile(info, body)


def _extract_archive(archive: Path, destination: Path) -> tuple[str, set[str]]:
    tree_sha256: str | None = None
    seen_names: set[str] = set()
    seen_relatives: set[str] = set()
    total = 0
    with tarfile.open(
        archive, mode="r:gz", encoding="utf-8", errors="strict"
    ) as source:
        for member in source:
            parsed_tree, relative = _parse_member_name(member.name)
            if tree_sha256 is None:
                tree_sha256 = parsed_tree
            if (
                parsed_tree != tree_sha256
                or not member.isfile()
                or member.name in seen_names
                or relative in seen_relatives
                or member.mode not in {0o644, 0o755}
                or (relative == "manifest.json" and member.mode != 0o644)
                or member.mtime != 0
                or member.uid != 0
                or member.gid != 0
                or member.uname
                or member.gname
                or member.size < 0
                or len(seen_names) >= _MAX_MEMBERS
                or member.size > _MAX_UNCOMPRESSED_BYTES - total
            ):
                raise CorpusError("snapshot archive metadata is invalid")
            target = destination / _ARCHIVE_ROOT / parsed_tree / relative
            target.parent.mkdir(parents=True, exist_ok=True)
            body = source.extractfile(member)
            if body is None:
                raise CorpusError("snapshot archive member is unreadable")
            with body, target.open("xb") as output:
                copied = _copy_exact(body, output, member.size)
            if copied != member.size:
                raise CorpusError("snapshot archive member size is invalid")
            target.chmod(member.mode)
            total += copied
            seen_names.add(member.name)
            seen_relatives.add(relative)
    if tree_sha256 is None or not seen_names:
        raise CorpusError("snapshot archive is empty")
    return tree_sha256, seen_relatives


def _parse_member_name(name: str) -> tuple[str, str]:
    path = PurePosixPath(name)
    if path.is_absolute() or len(path.parts) < 3 or path.parts[0] != _ARCHIVE_ROOT:
        raise CorpusError("snapshot archive member path is invalid")
    tree_sha256 = path.parts[1]
    if not _sha256(tree_sha256):
        raise CorpusError("snapshot archive tree identity is invalid")
    relative = PurePosixPath(*path.parts[2:]).as_posix()
    if relative != "manifest.json":
        if not relative.startswith("workspace/"):
            raise CorpusError("snapshot archive member path is invalid")
        safe_relative_path(relative.removeprefix("workspace/"))
    return tree_sha256, relative


def _copy_exact(source: Any, destination: Any, expected: int) -> int:
    copied = 0
    while copied < expected:
        block = source.read(min(1024 * 1024, expected - copied))
        if not block:
            break
        destination.write(block)
        copied += len(block)
    if source.read(1):
        raise CorpusError("snapshot archive member exceeds declared size")
    return copied


def _file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        while block := source.read(1024 * 1024):
            digest.update(block)
    return digest.hexdigest()


def _sha256(value: object) -> bool:
    return (
        isinstance(value, str)
        and len(value) == 64
        and all(character in "0123456789abcdef" for character in value)
    )


__all__ = [
    "SNAPSHOT_ARCHIVE_SCHEMA",
    "SnapshotArchiveReceipt",
    "build_snapshot_archive",
    "verify_snapshot_archive",
]
