"""Validate sanitized v2 snapshot capsules before trusted materialization."""

from __future__ import annotations

import hashlib
import io
import json
import re
import tarfile
from pathlib import PurePosixPath
from typing import Any

from ditto.api_models.coding_canonical import coding_canonical_json_bytes

_MAX_CAPSULE_BYTES = 128 << 20
_MAX_MANIFEST_BYTES = 8 << 20
_FORBIDDEN = {
    ".git",
    ".github",
    ".venv",
    "__pycache__",
    ".pytest_cache",
    "node_modules",
    "target",
    ".idea",
    ".vscode",
}


class PrivateSnapshotError(ValueError):
    """Safe error without source paths or private file contents."""


def validate_private_snapshot_capsule(
    body: bytes, *, expected_manifest_sha256: str | None = None
) -> None:
    """Check self-consistency, not authorization; callers must bind object hashes.

    Private plaintext stays inside the trusted Platform/curator boundary. This
    function extracts nothing to disk and returns no source material.
    """
    try:
        if not 0 < len(body) <= _MAX_CAPSULE_BYTES:
            raise ValueError("capsule bounds")
        with tarfile.open(fileobj=io.BytesIO(body), mode="r:") as archive:
            members: dict[str, tarfile.TarInfo] = {}
            for member in archive:
                if (
                    member.type not in {tarfile.REGTYPE, tarfile.AREGTYPE}
                    or member.name in members
                    or not 0 <= member.size <= _MAX_CAPSULE_BYTES
                    or member.uid != 0
                    or member.gid != 0
                    or member.uname
                    or member.gname
                    or member.mtime != 0
                    or set(member.pax_headers) - {"path"}
                ):
                    raise ValueError("capsule entry")
                members[member.name] = member
                if len(members) > 100_001:
                    raise ValueError("capsule entry count")
            if len(body) % tarfile.RECORDSIZE or any(body[archive.offset :]):
                raise ValueError("capsule trailer")
            header = members.get("manifest.json")
            if (
                header is None
                or not 0 < header.size <= _MAX_MANIFEST_BYTES
                or header.mode not in {0o600, 0o644}
            ):
                raise ValueError("manifest bounds")
            stream = archive.extractfile(header)
            if stream is None:
                raise ValueError("manifest")
            manifest_body = stream.read(_MAX_MANIFEST_BYTES + 1)
            if expected_manifest_sha256 is not None and (
                not re.fullmatch(r"[0-9a-f]{64}", expected_manifest_sha256)
                or hashlib.sha256(manifest_body).hexdigest() != expected_manifest_sha256
            ):
                raise ValueError("manifest binding")
            manifest: Any = json.loads(manifest_body)
            if (
                not isinstance(manifest, dict)
                or set(manifest)
                != {
                    "schema",
                    "files",
                    "source_tree_sha256",
                    "snapshot_tree_sha256",
                    "excluded_root_entries",
                }
                or manifest["schema"] != "dittobench-coding-sanitized-snapshot-v2"
                or coding_canonical_json_bytes(
                    manifest, maximum_bytes=_MAX_MANIFEST_BYTES, label="snapshot"
                )
                != manifest_body
                or not isinstance(manifest["files"], list)
                or not 0 <= len(manifest["files"]) <= 100_000
                or not isinstance(manifest["excluded_root_entries"], list)
                or any(
                    not isinstance(item, str)
                    for item in manifest["excluded_root_entries"]
                )
                or manifest["excluded_root_entries"]
                != sorted(set(manifest["excluded_root_entries"]))
                or not set(manifest["excluded_root_entries"]) <= {".git", ".github"}
            ):
                raise ValueError("manifest shape")
            paths: list[str] = []
            for entry in manifest["files"]:
                if not isinstance(entry, dict) or set(entry) != {
                    "mode",
                    "path",
                    "sha256",
                    "size_bytes",
                }:
                    raise ValueError("file identity")
                path = entry["path"]
                if not isinstance(path, str) or not _safe_path(path):
                    raise ValueError("file path")
                if (
                    type(entry["mode"]) is not int
                    or entry["mode"] not in {0o644, 0o755}
                    or type(entry["size_bytes"]) is not int
                    or not 0 <= entry["size_bytes"] <= _MAX_CAPSULE_BYTES
                    or not isinstance(entry["sha256"], str)
                    or not re.fullmatch(r"[0-9a-f]{64}", entry["sha256"])
                ):
                    raise ValueError("file identity")
                file_member = members.get("workspace/" + path)
                if (
                    file_member is None
                    or file_member.mode != entry["mode"]
                    or file_member.size != entry["size_bytes"]
                ):
                    raise ValueError("file metadata drift")
                content = archive.extractfile(file_member)
                if (
                    content is None
                    or hashlib.sha256(content.read(_MAX_CAPSULE_BYTES + 1)).hexdigest()
                    != entry["sha256"]
                ):
                    raise ValueError("file content drift")
                paths.append(path)
            if (
                len(paths) != len(set(paths))
                or paths != sorted(paths, key=PurePosixPath)
                or set(members)
                != {"manifest.json", *("workspace/" + path for path in paths)}
            ):
                raise ValueError("file set drift")
            tree_sha = hashlib.sha256(
                coding_canonical_json_bytes(
                    manifest["files"],
                    maximum_bytes=_MAX_MANIFEST_BYTES,
                    label="snapshot tree",
                )
            ).hexdigest()
            if (
                manifest["source_tree_sha256"] != tree_sha
                or manifest["snapshot_tree_sha256"] != tree_sha
            ):
                raise ValueError("tree identity drift")
    except Exception:
        raise PrivateSnapshotError(
            "private snapshot capsule verification failed"
        ) from None


def _safe_path(raw: str) -> bool:
    path = PurePosixPath(raw)
    return (
        bool(raw)
        and bool(path.parts)
        and "\\" not in raw
        and not path.is_absolute()
        and path.as_posix() == raw
        and ".." not in path.parts
        and not any(
            part in _FORBIDDEN or part == ".env" or part.startswith(".env.")
            for part in path.parts
        )
    )
