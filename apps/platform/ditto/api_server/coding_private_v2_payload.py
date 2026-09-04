"""Offline sealed-payload builder for verified private v2 catalog leaves."""

from __future__ import annotations

import hashlib
import io
import json
import stat
import tarfile
from pathlib import Path
from typing import Any

from ditto.api_models.coding_canonical import coding_canonical_json_bytes
from ditto.api_server.coding_private_catalog_v2_compile import (
    PrivateCatalogV2CompileError,
    verify_private_catalog_v2,
)


class PrivateV2PayloadError(ValueError):
    """The verified catalog cannot be materialized into a sealed payload."""


def build_private_v2_payload(
    *, catalog_directory: Path, groups_root: Path, output: Path
) -> dict[str, Any]:
    """Copy only runtime artifacts into a create-only content-addressed bundle."""

    try:
        catalog = verify_private_catalog_v2(catalog_directory)
    except PrivateCatalogV2CompileError as error:
        raise PrivateV2PayloadError("private v2 catalog is invalid") from error
    if groups_root.is_symlink() or not groups_root.is_dir():
        raise PrivateV2PayloadError("private v2 groups root is invalid")
    _new_directory(output)
    objects_dir = output / "objects"
    objects_dir.mkdir(mode=0o700)
    objects: dict[str, dict[str, Any]] = {}
    task_assets: list[dict[str, Any]] = []
    for record_meta in catalog["records"]:
        index = record_meta["catalog_index"]
        record = _read_json(catalog_directory / "records" / f"{index:06d}.json")
        group = groups_root / record["base_task_group_id"]
        condition = record["condition"]
        artifacts = {
            "catalog_record": _read_bytes(
                catalog_directory / "records" / f"{index:06d}.json"
            ),
            "visible_bundle": _archive_tree(group / "snapshot"),
            "issue": _read_bytes(group / "issue.json"),
            "runtime_policy": _read_bytes(group / "runtime-policy.json"),
            "resource_profile": _read_bytes(group / "resource-profile.json"),
            "memory_bundle": _read_bytes(group / "memory" / f"{condition}.json"),
            "grader_bundle": _archive_tree(group / "grader"),
        }
        identities: dict[str, str] = {}
        for role, body in artifacts.items():
            digest = hashlib.sha256(body).hexdigest()
            identities[role] = digest
            existing = objects.get(digest)
            if existing is None:
                _write_new(objects_dir / f"{digest}.bin", body)
                objects[digest] = {
                    "sha256": digest,
                    "size_bytes": len(body),
                }
            elif existing["size_bytes"] != len(body):
                raise PrivateV2PayloadError("content-addressed payload collision")
        task_assets.append(
            {
                "catalog_index": index,
                "task_version_id": record["task_version_id"],
                "task_commitment_sha256": record["task_commitment_sha256"],
                "artifacts": identities,
            }
        )
    projection = {
        "schema": "dittobench-coding-private-payload-v2",
        "coding_contract_version": 2,
        "weight_eligible": False,
        "catalog_sha256": catalog["catalog_sha256"],
        "catalog_merkle_root": catalog["catalog_merkle_root"],
        "task_version_count": catalog["task_version_count"],
        "objects": [objects[digest] for digest in sorted(objects)],
        "task_assets": task_assets,
    }
    authority = {
        **projection,
        "payload_sha256": _digest(projection, "private v2 payload authority"),
    }
    _write_new(
        output / "payload-authority.json",
        coding_canonical_json_bytes(
            authority, maximum_bytes=8 << 20, label="private v2 payload authority"
        ),
    )
    return authority


def verify_private_v2_payload(directory: Path) -> dict[str, Any]:
    """Verify every protected payload object without decrypting or uploading."""

    authority = _read_json(directory / "payload-authority.json")
    expected = {
        "schema",
        "coding_contract_version",
        "weight_eligible",
        "catalog_sha256",
        "catalog_merkle_root",
        "task_version_count",
        "objects",
        "task_assets",
        "payload_sha256",
    }
    projection = dict(authority)
    payload_sha = projection.pop("payload_sha256", None)
    if (
        set(authority) != expected
        or authority["schema"] != "dittobench-coding-private-payload-v2"
        or authority["coding_contract_version"] != 2
        or authority["weight_eligible"] is not False
        or authority["task_version_count"] != 250
        or not isinstance(payload_sha, str)
        or _digest(projection, "private v2 payload authority") != payload_sha
        or not isinstance(authority["objects"], list)
        or not isinstance(authority["task_assets"], list)
        or len(authority["task_assets"]) != 250
    ):
        raise PrivateV2PayloadError("private v2 payload authority is invalid")
    objects: dict[str, int] = {}
    for item in authority["objects"]:
        if not isinstance(item, dict) or set(item) != {"sha256", "size_bytes"}:
            raise PrivateV2PayloadError("private v2 payload object is invalid")
        digest = item["sha256"]
        size = item["size_bytes"]
        if (
            not isinstance(digest, str)
            or len(digest) != 64
            or type(size) is not int
            or size < 1
        ):
            raise PrivateV2PayloadError("private v2 payload object is invalid")
        body = _read_bytes(directory / "objects" / f"{digest}.bin")
        if len(body) != size or hashlib.sha256(body).hexdigest() != digest:
            raise PrivateV2PayloadError("private v2 payload object drifted")
        objects[digest] = size
    for index, task in enumerate(authority["task_assets"]):
        if not isinstance(task, dict) or task.get("catalog_index") != index:
            raise PrivateV2PayloadError("private v2 payload task order is invalid")
        artifacts = task.get("artifacts")
        if (
            not isinstance(artifacts, dict)
            or set(artifacts)
            != {
                "catalog_record",
                "visible_bundle",
                "issue",
                "runtime_policy",
                "resource_profile",
                "memory_bundle",
                "grader_bundle",
            }
            or any(value not in objects for value in artifacts.values())
        ):
            raise PrivateV2PayloadError("private v2 payload task assets are invalid")
    return authority


def _archive_tree(root: Path) -> bytes:
    if root.is_symlink() or not root.is_dir():
        raise PrivateV2PayloadError("private v2 artifact tree is invalid")
    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode="w") as archive:
        for path in sorted(root.rglob("*"), key=lambda item: item.as_posix()):
            if path.is_dir():
                continue
            if path.is_symlink() or not path.is_file():
                raise PrivateV2PayloadError("private v2 artifact tree is unsafe")
            info = tarfile.TarInfo(path.relative_to(root).as_posix())
            info.size = path.stat().st_size
            info.mode = stat.S_IMODE(path.stat().st_mode)
            info.mtime = 0
            info.uid = 0
            info.gid = 0
            info.uname = ""
            info.gname = ""
            with path.open("rb") as source:
                archive.addfile(info, source)
    return buffer.getvalue()


def _read_json(path: Path) -> dict[str, Any]:
    body = _read_bytes(path)
    try:
        value: Any = json.loads(body)
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise PrivateV2PayloadError("private v2 JSON artifact is invalid") from error
    if not isinstance(value, dict):
        raise PrivateV2PayloadError("private v2 JSON artifact is invalid")
    return value


def _read_bytes(path: Path) -> bytes:
    if path.is_symlink() or not path.is_file() or path.stat().st_size > 64 << 20:
        raise PrivateV2PayloadError("private v2 artifact is invalid")
    try:
        return path.read_bytes()
    except OSError as error:
        raise PrivateV2PayloadError("private v2 artifact is unreadable") from error


def _new_directory(path: Path) -> None:
    if (
        path.exists()
        or path.is_symlink()
        or path.parent.is_symlink()
        or not path.parent.is_dir()
        or stat.S_IMODE(path.parent.stat().st_mode) & 0o077
    ):
        raise PrivateV2PayloadError("private v2 payload output is unsafe")
    path.mkdir(mode=0o700)


def _write_new(path: Path, body: bytes) -> None:
    try:
        with path.open("xb") as output:
            output.write(body)
        path.chmod(0o600)
    except OSError as error:
        raise PrivateV2PayloadError("private v2 payload write failed") from error


def _digest(value: dict[str, Any], label: str) -> str:
    return hashlib.sha256(
        coding_canonical_json_bytes(value, maximum_bytes=8 << 20, label=label)
    ).hexdigest()
