"""Deterministic, Git-free source snapshots for Coding practice and curation."""

from __future__ import annotations

import os
import shutil
import stat
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from dittobench_coding_datagen.canonical import (
    canonical_json_bytes,
    safe_relative_path,
    sha256_hex,
    tree_identities,
)
from dittobench_coding_datagen.model import CorpusError

SNAPSHOT_SCHEMA = "dittobench-coding-sanitized-snapshot-v1"
_ROOT_CONTROL_EXCLUSIONS = frozenset({".git", ".github"})
_CACHE_DIRECTORIES = frozenset(
    {
        ".idea",
        ".pytest_cache",
        ".venv",
        ".vscode",
        "__pycache__",
        "node_modules",
        "target",
    }
)
_MAX_FILE_BYTES = 1 << 30


@dataclass(frozen=True)
class SanitizedSnapshot:
    schema: str
    source_tree_sha256: str
    excluded_root_entries: tuple[str, ...]
    files: tuple[dict[str, Any], ...]
    snapshot_tree_sha256: str

    def as_json(self) -> dict[str, object]:
        return {
            "excluded_root_entries": list(self.excluded_root_entries),
            "files": list(self.files),
            "schema": self.schema,
            "snapshot_tree_sha256": self.snapshot_tree_sha256,
            "source_tree_sha256": self.source_tree_sha256,
        }


def export_sanitized_snapshot(source: Path, output: Path) -> SanitizedSnapshot:
    """Copy regular source files into a normalized, Git-free snapshot directory."""

    if (
        source.is_symlink()
        or not source.is_dir()
        or output.is_symlink()
        or output.exists()
        or output.resolve().is_relative_to(source.resolve())
    ):
        raise CorpusError("sanitized snapshot source or output is unsafe")
    output.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(
        prefix=f".{output.name}.", dir=output.parent
    ) as raw:
        staged = Path(raw) / "snapshot"
        workspace = staged / "workspace"
        workspace.mkdir(parents=True)
        excluded = _copy_source(source, workspace)
        identities = tuple(
            identity.as_json() for identity in tree_identities(workspace)
        )
        source_tree_sha256 = sha256_hex(canonical_json_bytes(list(identities)))
        snapshot = SanitizedSnapshot(
            schema=SNAPSHOT_SCHEMA,
            source_tree_sha256=source_tree_sha256,
            excluded_root_entries=tuple(sorted(excluded)),
            files=identities,
            snapshot_tree_sha256=source_tree_sha256,
        )
        manifest = canonical_json_bytes(snapshot.as_json())
        (staged / "manifest.json").write_bytes(manifest)
        os.replace(staged, output)
    return snapshot


def _copy_source(source: Path, destination: Path) -> set[str]:
    excluded: set[str] = set()
    for root, directories, files in os.walk(source, topdown=True, followlinks=False):
        root_path = Path(root)
        relative_root = root_path.relative_to(source)
        if relative_root != Path("."):
            safe_relative_path(relative_root.as_posix())
        retained_directories: list[str] = []
        for name in sorted(directories):
            path = root_path / name
            relative = path.relative_to(source).as_posix()
            if path.is_symlink():
                raise CorpusError(f"source snapshot contains a symlink: {relative}")
            if relative_root == Path(".") and name in _ROOT_CONTROL_EXCLUSIONS:
                excluded.add(name)
                continue
            if name in _CACHE_DIRECTORIES:
                continue
            safe_relative_path(relative)
            retained_directories.append(name)
        directories[:] = retained_directories
        for name in sorted(files):
            path = root_path / name
            relative = path.relative_to(source).as_posix()
            if relative_root == Path(".") and name in _ROOT_CONTROL_EXCLUSIONS:
                excluded.add(name)
                continue
            if name in _CACHE_DIRECTORIES:
                continue
            if name == ".env" or name.startswith(".env."):
                raise CorpusError(
                    f"source snapshot contains a credential file: {relative}"
                )
            safe_relative_path(relative)
            info = path.lstat()
            if path.is_symlink():
                raise CorpusError(f"source snapshot contains a symlink: {relative}")
            if not stat.S_ISREG(info.st_mode):
                raise CorpusError(
                    f"source snapshot contains a non-regular file: {relative}"
                )
            if info.st_size < 0 or info.st_size > _MAX_FILE_BYTES:
                raise CorpusError(
                    f"source snapshot file size is outside bounds: {relative}"
                )
            target = destination / relative
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(path, target, follow_symlinks=False)
            target.chmod(0o755 if info.st_mode & stat.S_IXUSR else 0o644)
    return excluded


__all__ = ["SNAPSHOT_SCHEMA", "SanitizedSnapshot", "export_sanitized_snapshot"]
