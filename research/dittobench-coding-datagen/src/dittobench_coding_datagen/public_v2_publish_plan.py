"""Deterministic, credential-free publication plans for public Coding v2."""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

from dittobench_coding_datagen.canonical import (
    canonical_json_bytes,
    safe_opaque_id,
    sha256_hex,
)
from dittobench_coding_datagen.model import CorpusError
from dittobench_coding_datagen.public_v2_release import verify_public_v2_release

PUBLIC_V2_PUBLISH_PLAN_SCHEMA = "dittobench-coding-public-v2-publish-plan-v1"
_DATASET_REPOSITORY = re.compile(
    r"^[A-Za-z0-9][A-Za-z0-9._-]{0,95}/[A-Za-z0-9][A-Za-z0-9._-]{0,95}$"
)
_REVISION = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._/-]{0,127}$")


def build_public_v2_publish_plan(
    *, release_dir: Path, dataset_repository: str, revision: str
) -> dict[str, Any]:
    """Bind one verified release directory to immutable public upload paths."""

    if (
        release_dir.is_symlink()
        or not release_dir.is_dir()
        or not _DATASET_REPOSITORY.fullmatch(dataset_repository)
        or not _REVISION.fullmatch(revision)
    ):
        raise CorpusError("public v2 publication target is invalid")
    descriptors = sorted(release_dir.glob("*.release.json"))
    if len(descriptors) != 1 or descriptors[0].is_symlink():
        raise CorpusError("public v2 publication descriptor is invalid")
    descriptor_path = descriptors[0]
    descriptor = _load_descriptor(descriptor_path)
    release_id = safe_opaque_id(descriptor["public_release_id"])
    archive_path = release_dir / str(descriptor["archive_name"])
    manifest_path = release_dir / "manifest.json"
    verified = verify_public_v2_release(
        archive=archive_path,
        descriptor=descriptor_path,
    )
    if verified != descriptor:
        raise CorpusError("public v2 publication descriptor drifted")
    manifest = _read_regular(manifest_path, "public v2 publication manifest")
    manifest_sha256 = sha256_hex(manifest)
    if manifest_sha256 != descriptor["pack_manifest_sha256"]:
        raise CorpusError("public v2 publication manifest does not match descriptor")
    prefix = f"releases/{release_id}/{manifest_sha256}"
    files = tuple(
        sorted(
            (
                _publication_file(
                    path=path,
                    remote_path=f"{prefix}/{path.name}",
                )
                for path in (archive_path, descriptor_path, manifest_path)
            ),
            key=lambda item: str(item["remote_path"]),
        )
    )
    return {
        "archive_sha256": descriptor["archive_sha256"],
        "dataset_repository": dataset_repository,
        "files": list(files),
        "pack_manifest_sha256": manifest_sha256,
        "public_release_id": release_id,
        "revision": revision,
        "schema": PUBLIC_V2_PUBLISH_PLAN_SCHEMA,
        "upload_prefix": prefix,
        "weight_eligible": False,
    }


def canonical_public_v2_publish_plan_bytes(plan: dict[str, Any]) -> bytes:
    """Validate and encode one non-secret publication plan canonically."""

    _validate_plan(plan)
    return canonical_json_bytes(plan)


def _publication_file(*, path: Path, remote_path: str) -> dict[str, object]:
    body = _read_regular(path, "public v2 publication artifact")
    return {
        "remote_path": remote_path,
        "sha256": sha256_hex(body),
        "size_bytes": len(body),
    }


def _load_descriptor(path: Path) -> dict[str, Any]:
    import json

    try:
        value: Any = json.loads(_read_regular(path, "public v2 publication descriptor"))
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise CorpusError("public v2 publication descriptor is invalid") from error
    if not isinstance(value, dict):
        raise CorpusError("public v2 publication descriptor is invalid")
    return {str(key): item for key, item in value.items()}


def _read_regular(path: Path, label: str) -> bytes:
    if path.is_symlink() or not path.is_file() or path.stat().st_size > 2 << 30:
        raise CorpusError(f"{label} is invalid")
    try:
        return path.read_bytes()
    except OSError as error:
        raise CorpusError(f"{label} is unreadable") from error


def _validate_plan(plan: dict[str, Any]) -> None:
    expected = {
        "archive_sha256",
        "dataset_repository",
        "files",
        "pack_manifest_sha256",
        "public_release_id",
        "revision",
        "schema",
        "upload_prefix",
        "weight_eligible",
    }
    if set(plan) != expected or plan["schema"] != PUBLIC_V2_PUBLISH_PLAN_SCHEMA:
        raise CorpusError("public v2 publication plan is invalid")
    if (
        not isinstance(plan["dataset_repository"], str)
        or not _DATASET_REPOSITORY.fullmatch(plan["dataset_repository"])
        or not isinstance(plan["revision"], str)
        or not _REVISION.fullmatch(plan["revision"])
        or plan["weight_eligible"] is not False
    ):
        raise CorpusError("public v2 publication plan is invalid")
    release_id = safe_opaque_id(plan["public_release_id"])
    if (
        not _sha256(plan["archive_sha256"])
        or not _sha256(plan["pack_manifest_sha256"])
        or plan["upload_prefix"]
        != f"releases/{release_id}/{plan['pack_manifest_sha256']}"
    ):
        raise CorpusError("public v2 publication plan is invalid")
    files = plan["files"]
    if not isinstance(files, list) or len(files) != 3:
        raise CorpusError("public v2 publication plan is invalid")
    remote_paths: list[str] = []
    for item in files:
        if (
            not isinstance(item, dict)
            or set(item) != {"remote_path", "sha256", "size_bytes"}
            or not isinstance(item["remote_path"], str)
            or not item["remote_path"].startswith(f"{plan['upload_prefix']}/")
            or not _sha256(item["sha256"])
            or type(item["size_bytes"]) is not int
            or item["size_bytes"] < 1
        ):
            raise CorpusError("public v2 publication plan is invalid")
        remote_paths.append(item["remote_path"])
    if remote_paths != sorted(set(remote_paths)):
        raise CorpusError("public v2 publication plan is invalid")


def _sha256(value: object) -> bool:
    return (
        isinstance(value, str)
        and len(value) == 64
        and all(character in "0123456789abcdef" for character in value)
    )


__all__ = [
    "PUBLIC_V2_PUBLISH_PLAN_SCHEMA",
    "build_public_v2_publish_plan",
    "canonical_public_v2_publish_plan_bytes",
]
