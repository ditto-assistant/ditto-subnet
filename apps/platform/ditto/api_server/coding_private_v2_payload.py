"""Offline sealed-payload builder for verified private v2 catalog leaves."""

from __future__ import annotations

import hashlib
import io
import json
import re
import stat
import tarfile
from pathlib import Path
from typing import Any

from ditto.api_models.coding_canonical import coding_canonical_json_bytes
from ditto.api_models.coding_private_catalog_v2 import CodingPrivateCatalogV2Task
from ditto.api_server.coding_private_catalog_v2_compile import (
    PrivateCatalogV2CompileError,
    verify_private_catalog_v2,
)

_OPAQUE_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")


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
        record_path = catalog_directory / "records" / f"{index:06d}.json"
        record = _canonical_json(record_path, "private v2 catalog record")
        group_id = _safe_path_id(record["base_task_group_id"])
        group = groups_root / group_id
        condition = record["condition"]
        memory_body = _read_bytes(group / "memory" / f"{condition}.json")
        runtime_body = _read_bytes(group / "runtime-policy.json")
        resource_body = _read_bytes(group / "resource-profile.json")
        snapshot_root = group / "snapshot"
        grader_root = group / "grader"
        if (
            hashlib.sha256(memory_body).hexdigest() != record["memory_bundle_sha256"]
            or hashlib.sha256(runtime_body).hexdigest()
            != record["runtime_policy_sha256"]
            or hashlib.sha256(resource_body).hexdigest()
            != record["resource_profile_sha256"]
            or _tree_digest(snapshot_root) != record["visible_snapshot_tree_sha256"]
            or _tree_digest(grader_root) != record["hidden_grader_tree_sha256"]
        ):
            raise PrivateV2PayloadError("private v2 payload artifacts drifted")
        artifacts = {
            "catalog_record": _read_bytes(record_path),
            "visible_bundle": _archive_tree(snapshot_root),
            "issue": _read_bytes(group / "issue.json"),
            "runtime_policy": runtime_body,
            "resource_profile": resource_body,
            "memory_bundle": memory_body,
            "grader_bundle": _archive_tree(grader_root),
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

    authority = _canonical_json(
        directory / "payload-authority.json", "private v2 payload authority"
    )
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
    objects_dir = directory / "objects"
    if objects_dir.is_symlink() or not objects_dir.is_dir():
        raise PrivateV2PayloadError("private v2 payload objects are invalid")
    on_disk: set[str] = set()
    for path in objects_dir.iterdir():
        if path.is_symlink() or not path.is_file() or not path.name.endswith(".bin"):
            raise PrivateV2PayloadError("private v2 payload objects drifted")
        on_disk.add(path.name[: -len(".bin")])
    if on_disk != set(objects):
        raise PrivateV2PayloadError("private v2 payload objects drifted")
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
        record_body = _read_bytes(
            directory / "objects" / f"{artifacts['catalog_record']}.bin"
        )
        task = CodingPrivateCatalogV2Task.model_validate(json.loads(record_body))
        memory_body = _read_bytes(
            directory / "objects" / f"{artifacts['memory_bundle']}.bin"
        )
        runtime_body = _read_bytes(
            directory / "objects" / f"{artifacts['runtime_policy']}.bin"
        )
        resource_body = _read_bytes(
            directory / "objects" / f"{artifacts['resource_profile']}.bin"
        )
        visible_body = _read_bytes(
            directory / "objects" / f"{artifacts['visible_bundle']}.bin"
        )
        grader_body = _read_bytes(
            directory / "objects" / f"{artifacts['grader_bundle']}.bin"
        )
        if (
            artifacts["memory_bundle"] != task.memory_bundle_sha256
            or artifacts["runtime_policy"] != task.runtime_policy_sha256
            or artifacts["resource_profile"] != task.resource_profile_sha256
            or hashlib.sha256(memory_body).hexdigest() != task.memory_bundle_sha256
            or hashlib.sha256(runtime_body).hexdigest() != task.runtime_policy_sha256
            or hashlib.sha256(resource_body).hexdigest() != task.resource_profile_sha256
            or _tree_digest_from_tar(visible_body) != task.visible_snapshot_tree_sha256
            or _tree_digest_from_tar(grader_body) != task.hidden_grader_tree_sha256
        ):
            raise PrivateV2PayloadError("private v2 payload artifacts drifted")
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


def _canonical_json(path: Path, label: str) -> dict[str, Any]:
    body = _read_bytes(path)
    try:
        value: Any = json.loads(body)
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise PrivateV2PayloadError(f"{label} is invalid") from error
    if (
        not isinstance(value, dict)
        or coding_canonical_json_bytes(value, maximum_bytes=8 << 20, label=label)
        != body
    ):
        raise PrivateV2PayloadError(f"{label} is not canonical")
    return value


def _tree_digest(root: Path) -> str:
    if root.is_symlink() or not root.is_dir():
        raise PrivateV2PayloadError("private v2 artifact tree is invalid")
    identities: list[dict[str, Any]] = []
    for path in sorted(root.rglob("*")):
        if path.is_dir():
            continue
        if path.is_symlink() or not path.is_file():
            raise PrivateV2PayloadError("private v2 artifact tree is unsafe")
        body = path.read_bytes()
        identities.append(
            {
                "path": path.relative_to(root).as_posix(),
                "sha256": hashlib.sha256(body).hexdigest(),
                "size_bytes": len(body),
            }
        )
    return hashlib.sha256(
        coding_canonical_json_bytes(
            identities, maximum_bytes=4 << 20, label="private v2 tree identity"
        )
    ).hexdigest()


def _tree_digest_from_tar(body: bytes) -> str:
    identities: list[dict[str, Any]] = []
    try:
        with tarfile.open(fileobj=io.BytesIO(body), mode="r:") as archive:
            for member in sorted(archive.getmembers(), key=lambda item: item.name):
                if member.isdir():
                    continue
                if not member.isfile() or member.issym() or member.islnk():
                    raise PrivateV2PayloadError("private v2 artifact tree is unsafe")
                extracted = archive.extractfile(member)
                if extracted is None:
                    raise PrivateV2PayloadError("private v2 artifact tree is invalid")
                content = extracted.read()
                identities.append(
                    {
                        "path": member.name,
                        "sha256": hashlib.sha256(content).hexdigest(),
                        "size_bytes": len(content),
                    }
                )
    except tarfile.TarError as error:
        raise PrivateV2PayloadError("private v2 artifact tree is invalid") from error
    return hashlib.sha256(
        coding_canonical_json_bytes(
            identities, maximum_bytes=4 << 20, label="private v2 tree identity"
        )
    ).hexdigest()


def _safe_path_id(value: object) -> str:
    if not isinstance(value, str) or not _OPAQUE_ID.fullmatch(value):
        raise PrivateV2PayloadError("private group identity is unsafe")
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
