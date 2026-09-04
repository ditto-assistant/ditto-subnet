"""Canonical bytes and content identities for coding packs."""

from __future__ import annotations

import hashlib
import json
import re
from pathlib import Path, PurePosixPath
from typing import Any

from dittobench_coding_datagen.model import CorpusError, FileIdentity

_OPAQUE_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")


def canonical_json_bytes(value: Any) -> bytes:
    """Return the only JSON byte representation admitted by the contract."""

    return (
        (
            json.dumps(
                value,
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
                allow_nan=False,
            )
            + "\n"
        )
        .replace("\u2028", "\\u2028")
        .replace("\u2029", "\\u2029")
        .encode("utf-8")
    )


def sha256_hex(body: bytes) -> str:
    return hashlib.sha256(body).hexdigest()


def safe_opaque_id(value: object) -> str:
    """Reject identifiers that cannot be used as a single path component."""

    if not isinstance(value, str) or not _OPAQUE_ID.fullmatch(value):
        raise CorpusError("identifier is unsafe")
    return value


def safe_relative_path(raw: str) -> str:
    """Normalize and validate a capsule-relative POSIX path."""

    if not raw or "\\" in raw:
        raise CorpusError(f"unsafe relative path: {raw!r}")
    path = PurePosixPath(raw)
    if path.is_absolute() or ".." in path.parts or str(path) != raw:
        raise CorpusError(f"unsafe relative path: {raw!r}")
    if any(part in {".git", ".github"} for part in path.parts):
        raise CorpusError(f"forbidden control path: {raw!r}")
    return path.as_posix()


def file_identity(root: Path, path: Path) -> FileIdentity:
    relative = path.relative_to(root).as_posix()
    safe_relative_path(relative)
    body = path.read_bytes()
    return FileIdentity(relative, len(body), sha256_hex(body))


def tree_identities(
    root: Path, *, exclude: frozenset[str] = frozenset()
) -> list[FileIdentity]:
    """Hash every regular file below root in stable path order."""

    if not root.is_dir() or root.is_symlink():
        raise CorpusError(f"pack root is not a real directory: {root}")
    identities: list[FileIdentity] = []
    for path in sorted(root.rglob("*")):
        if path.is_symlink():
            raise CorpusError(f"pack contains a symlink: {path.relative_to(root)}")
        if path.is_dir():
            continue
        if not path.is_file():
            raise CorpusError(
                f"pack contains a non-regular entry: {path.relative_to(root)}"
            )
        relative = path.relative_to(root).as_posix()
        if relative in exclude:
            continue
        identities.append(file_identity(root, path))
    return identities
