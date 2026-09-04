"""Immutable, public-only distribution artifacts for Coding practice v2."""

from __future__ import annotations

import gzip
import json
import os
import shutil
import tarfile
import tempfile
from pathlib import Path
from typing import Any

from dittobench_coding_datagen.canonical import (
    canonical_json_bytes,
    normalized_tree_identities,
    safe_opaque_id,
    safe_relative_path,
    sha256_hex,
)
from dittobench_coding_datagen.model import CorpusError
from dittobench_coding_datagen.public_pack_v2 import validate_public_v2_pack

PUBLIC_V2_RELEASE_SCHEMA = "dittobench-coding-public-release-v2"
_ARCHIVE_ROOT = "dittobench-public-v2"
_MAX_ARCHIVE_BYTES = 2 << 30
_MAX_DESCRIPTOR_BYTES = 1 << 20
_MAX_MEMBERS = 100_000


def build_public_v2_release(
    *, pack: Path, output: Path, replace: bool = False
) -> dict[str, Any]:
    """Build a deterministic public release directory from a validated v2 pack."""

    manifest = validate_public_v2_pack(pack)
    release_id = safe_opaque_id(manifest["public_release_id"])
    if output.is_symlink() or output.resolve().is_relative_to(pack.resolve()):
        raise CorpusError("public v2 release output is unsafe")
    if output.exists():
        if not replace or output.is_symlink() or not output.is_dir():
            raise CorpusError("public v2 release output already exists")
        shutil.rmtree(output)
    output.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(
        prefix=f".{output.name}.", dir=output.parent
    ) as raw:
        staged = Path(raw) / "release"
        staged.mkdir()
        archive_name = f"{release_id}.tar.gz"
        archive = staged / archive_name
        _write_archive(pack, archive, release_id=release_id)
        archive_body = archive.read_bytes()
        if not archive_body or len(archive_body) > _MAX_ARCHIVE_BYTES:
            raise CorpusError("public v2 archive size is outside bounds")
        manifest_body = (pack / "manifest.json").read_bytes()
        descriptor = {
            "archive_name": archive_name,
            "archive_sha256": sha256_hex(archive_body),
            "archive_size_bytes": len(archive_body),
            "local_result_schema": "dittobench-coding-local-practice-result-v2",
            "pack_manifest_sha256": sha256_hex(manifest_body),
            "public_release_id": release_id,
            "schema": PUBLIC_V2_RELEASE_SCHEMA,
            "weight_eligible": False,
        }
        descriptor_path = staged / f"{release_id}.release.json"
        descriptor_path.write_bytes(canonical_json_bytes(descriptor))
        (staged / "manifest.json").write_bytes(manifest_body)
        os.replace(staged, output)
    return verify_public_v2_release(
        archive=output / archive_name,
        descriptor=output / f"{release_id}.release.json",
    )


def verify_public_v2_release(*, archive: Path, descriptor: Path) -> dict[str, Any]:
    """Verify descriptor, archive bytes, and every extracted v2 pack identity."""

    if (
        archive.is_symlink()
        or descriptor.is_symlink()
        or not archive.is_file()
        or not descriptor.is_file()
    ):
        raise CorpusError("public v2 release artifact is unsafe")
    archive_body = archive.read_bytes()
    if not archive_body or len(archive_body) > _MAX_ARCHIVE_BYTES:
        raise CorpusError("public v2 archive size is outside bounds")
    try:
        descriptor_body = descriptor.read_bytes()
        if len(descriptor_body) > _MAX_DESCRIPTOR_BYTES:
            raise CorpusError("public v2 release descriptor is invalid")
        value = json.loads(descriptor_body)
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as error:
        raise CorpusError("public v2 release descriptor is invalid") from error
    if canonical_json_bytes(value) != descriptor_body:
        raise CorpusError("public v2 release descriptor is invalid")
    _validate_descriptor(value, archive_body)
    release_id = safe_opaque_id(value["public_release_id"])
    with tempfile.TemporaryDirectory(prefix="dittobench-public-v2-verify-") as raw:
        destination = Path(raw) / "pack"
        _extract_archive(archive, destination=destination, release_id=release_id)
        manifest_body = (destination / "manifest.json").read_bytes()
        if sha256_hex(manifest_body) != value["pack_manifest_sha256"]:
            raise CorpusError("public v2 release manifest does not match descriptor")
        manifest = validate_public_v2_pack(destination)
        if manifest["public_release_id"] != release_id:
            raise CorpusError("public v2 release identity does not match pack")
    return {str(key): item for key, item in value.items()}


def _write_archive(pack: Path, archive: Path, *, release_id: str) -> None:
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
        ) as tar,
    ):
        for identity in normalized_tree_identities(pack):
            relative = safe_relative_path(str(identity["path"]))
            info = tarfile.TarInfo(f"{_ARCHIVE_ROOT}/{release_id}/{relative}")
            info.size = int(identity["size_bytes"])
            info.mode = int(identity["mode"])
            info.mtime = 0
            info.uid = 0
            info.gid = 0
            info.uname = ""
            info.gname = ""
            with (pack / relative).open("rb") as source:
                tar.addfile(info, source)


def _extract_archive(archive: Path, *, destination: Path, release_id: str) -> None:
    prefix = f"{_ARCHIVE_ROOT}/{release_id}/"
    total = 0
    seen: set[str] = set()
    with tarfile.open(archive, mode="r:gz", encoding="utf-8", errors="strict") as tar:
        for member in tar:
            if (
                not member.isfile()
                or member.name in seen
                or not member.name.startswith(prefix)
                or member.mode not in {0o644, 0o755}
                or member.mtime != 0
                or member.uid != 0
                or member.gid != 0
                or member.uname
                or member.gname
                or member.size < 0
                or len(seen) >= _MAX_MEMBERS
                or member.size > _MAX_ARCHIVE_BYTES - total
            ):
                raise CorpusError("public v2 archive metadata is invalid")
            relative = safe_relative_path(member.name.removeprefix(prefix))
            target = destination / relative
            target.parent.mkdir(parents=True, exist_ok=True)
            source = tar.extractfile(member)
            if source is None:
                raise CorpusError("public v2 archive member is unreadable")
            with source, target.open("xb") as sink:
                copied = _copy_exact(source, sink, member.size)
            if copied != member.size:
                raise CorpusError("public v2 archive member size is invalid")
            target.chmod(member.mode)
            total += copied
            seen.add(member.name)
    if not seen:
        raise CorpusError("public v2 archive is empty")


def _copy_exact(source: Any, destination: Any, expected: int) -> int:
    copied = 0
    while copied < expected:
        block = source.read(min(1024 * 1024, expected - copied))
        if not block:
            break
        destination.write(block)
        copied += len(block)
    if source.read(1):
        raise CorpusError("public v2 archive member exceeds declared size")
    return copied


def _validate_descriptor(value: object, archive: bytes) -> None:
    if not isinstance(value, dict) or set(value) != {
        "archive_name",
        "archive_sha256",
        "archive_size_bytes",
        "local_result_schema",
        "pack_manifest_sha256",
        "public_release_id",
        "schema",
        "weight_eligible",
    }:
        raise CorpusError("public v2 release descriptor fields are invalid")
    if (
        value["schema"] != PUBLIC_V2_RELEASE_SCHEMA
        or value["local_result_schema"] != "dittobench-coding-local-practice-result-v2"
        or value["weight_eligible"] is not False
        or value["archive_name"]
        != f"{safe_opaque_id(value['public_release_id'])}.tar.gz"
        or value["archive_size_bytes"] != len(archive)
        or value["archive_sha256"] != sha256_hex(archive)
        or any(
            not isinstance(value[field], str)
            or len(value[field]) != 64
            or any(character not in "0123456789abcdef" for character in value[field])
            for field in ("archive_sha256", "pack_manifest_sha256")
        )
    ):
        raise CorpusError("public v2 release descriptor authority is invalid")


__all__ = [
    "PUBLIC_V2_RELEASE_SCHEMA",
    "build_public_v2_release",
    "verify_public_v2_release",
]
