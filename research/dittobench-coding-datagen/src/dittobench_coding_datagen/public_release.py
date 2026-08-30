"""Deterministic, public-only distribution artifacts for coding practice packs."""

from __future__ import annotations

import gzip
import json
import os
import re
import shutil
import tarfile
import tempfile
from pathlib import Path
from typing import Any

from dittobench_coding_datagen.canonical import (
    canonical_json_bytes,
    safe_relative_path,
    sha256_hex,
    tree_identities,
)
from dittobench_coding_datagen.model import CorpusError
from dittobench_coding_datagen.validation import validate_pack

_RELEASE_SCHEMA = "dittobench-coding-public-practice-release-v1"
_ARCHIVE_ROOT = "practice"
_MAX_ARCHIVE_BYTES = 64 << 20
_MAX_DESCRIPTOR_BYTES = 1 << 20
_MAX_UNCOMPRESSED_BYTES = 64 << 20
_MAX_ARCHIVE_MEMBERS = 4096
_PACK_ID = re.compile(r"^coding-practice-3x3-v[1-9][0-9]*$")
_DESCRIPTOR_KEYS = frozenset(
    {
        "archive_name",
        "archive_sha256",
        "archive_size_bytes",
        "coding_contract_version",
        "corpus_scope",
        "file_count",
        "generation_mode",
        "manifest_name",
        "manifest_sha256",
        "memory_count",
        "practice_pack_id",
        "release_notes_name",
        "schema",
        "source_sha256",
        "task_count",
        "task_entropy_bits",
        "user_count",
        "weight_eligible",
    }
)


def build_public_practice_release(
    pack: Path,
    output: Path,
    *,
    replace: bool = False,
) -> dict[str, Any]:
    """Build one deterministic, content-addressed public practice release."""

    pack = _real_directory(pack, label="practice pack")
    manifest = validate_pack(pack)
    output = _release_output(pack, output, replace=replace)
    pack_id = _string(manifest, "practice_pack_id")
    archive_name = f"{pack_id}.tar.gz"
    manifest_name = "manifest.json"
    release_notes_name = "RELEASE.md"
    descriptor_name = f"{pack_id}.release.json"

    with tempfile.TemporaryDirectory(
        prefix=f".{output.name}.", dir=output.parent
    ) as temporary:
        staged = Path(temporary) / "release"
        staged.mkdir()
        archive_path = staged / archive_name
        _write_archive(pack, archive_path, pack_id=pack_id)
        archive = archive_path.read_bytes()
        if len(archive) > _MAX_ARCHIVE_BYTES:
            raise CorpusError("public practice archive exceeds its 64 MiB bound")
        manifest_body = (pack / manifest_name).read_bytes()
        descriptor = {
            "archive_name": archive_name,
            "archive_sha256": sha256_hex(archive),
            "archive_size_bytes": len(archive),
            "coding_contract_version": manifest["coding_contract_version"],
            "corpus_scope": manifest["corpus_scope"],
            "file_count": len(manifest["files"]),
            "generation_mode": manifest["generation_mode"],
            "manifest_name": manifest_name,
            "manifest_sha256": sha256_hex(manifest_body),
            "memory_count": manifest["memory_count"],
            "practice_pack_id": pack_id,
            "release_notes_name": release_notes_name,
            "schema": _RELEASE_SCHEMA,
            "source_sha256": manifest["source_sha256"],
            "task_count": manifest["task_count"],
            "task_entropy_bits": manifest["task_entropy_bits"],
            "user_count": manifest["user_count"],
            "weight_eligible": False,
        }
        _validate_descriptor(descriptor)
        (staged / manifest_name).write_bytes(manifest_body)
        (staged / descriptor_name).write_bytes(canonical_json_bytes(descriptor))
        (staged / release_notes_name).write_text(
            _release_notes(descriptor), encoding="utf-8", newline="\n"
        )
        verify_public_practice_release(
            archive=archive_path,
            descriptor=staged / descriptor_name,
        )
        os.replace(staged, output)
    return descriptor


def verify_public_practice_release(
    *, archive: Path, descriptor: Path
) -> dict[str, Any]:
    """Verify archive bytes and every embedded public pack identity."""

    if archive.is_symlink() or not archive.is_file():
        raise CorpusError("public practice archive is not a regular file")
    if descriptor.is_symlink() or not descriptor.is_file():
        raise CorpusError("public practice descriptor is not a regular file")
    archive_body = archive.read_bytes()
    if not archive_body or len(archive_body) > _MAX_ARCHIVE_BYTES:
        raise CorpusError("public practice archive is outside its byte bound")
    try:
        with descriptor.open("rb") as stream:
            descriptor_body = stream.read(_MAX_DESCRIPTOR_BYTES + 1)
        if len(descriptor_body) > _MAX_DESCRIPTOR_BYTES:
            raise CorpusError("public practice descriptor exceeds its byte bound")
        decoded = json.loads(descriptor_body)
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as error:
        raise CorpusError(
            f"public practice descriptor is malformed: {error}"
        ) from error
    if not isinstance(decoded, dict):
        raise CorpusError("public practice descriptor must be an object")
    _validate_descriptor(decoded)
    if descriptor_body != canonical_json_bytes(decoded):
        raise CorpusError("public practice descriptor is not canonical JSON")
    if decoded["archive_size_bytes"] != len(archive_body) or decoded[
        "archive_sha256"
    ] != sha256_hex(archive_body):
        raise CorpusError("public practice descriptor does not match archive bytes")
    manifest_copy = descriptor.parent / str(decoded["manifest_name"])
    if manifest_copy.is_symlink() or not manifest_copy.is_file():
        raise CorpusError("public practice release manifest copy is unavailable")
    manifest_copy_body = manifest_copy.read_bytes()
    if sha256_hex(manifest_copy_body) != decoded["manifest_sha256"]:
        raise CorpusError("public practice descriptor does not match manifest copy")

    pack_id = str(decoded["practice_pack_id"])
    with tempfile.TemporaryDirectory(prefix="dittobench-coding-release-") as temporary:
        unpacked = Path(temporary) / _ARCHIVE_ROOT / pack_id
        _extract_archive(archive, destination=Path(temporary), pack_id=pack_id)
        try:
            archive_manifest_body = (unpacked / "manifest.json").read_bytes()
        except FileNotFoundError as error:
            raise CorpusError("public practice archive manifest is missing") from error
        if archive_manifest_body != manifest_copy_body:
            raise CorpusError("public practice archive manifest disagrees with copy")
        manifest = validate_pack(unpacked)
    _validate_descriptor_against_manifest(decoded, manifest)
    return decoded


def _real_directory(path: Path, *, label: str) -> Path:
    if path.is_symlink() or not path.is_dir():
        raise CorpusError(f"{label} is not a real directory: {path}")
    return path.resolve(strict=True)


def _release_output(pack: Path, output: Path, *, replace: bool) -> Path:
    if output.is_symlink():
        raise CorpusError(f"release output must not be a symlink: {output}")
    output = output.resolve()
    if _is_forbidden_replace_target(output):
        raise CorpusError("release output must not be a filesystem root or mountpoint")
    if output == pack or output.is_relative_to(pack):
        raise CorpusError("release output must not be inside the practice pack")
    if output.exists():
        if not replace:
            raise CorpusError(
                f"release output already exists: {output}; pass --replace"
            )
        if not output.is_dir():
            raise CorpusError(f"release output is not a directory: {output}")
        shutil.rmtree(output)
    output.parent.mkdir(parents=True, exist_ok=True)
    return output


def _is_forbidden_replace_target(path: Path) -> bool:
    if path.parent == path:
        return True
    try:
        return path.exists() and path.is_mount()
    except OSError:
        return True


def _write_archive(pack: Path, archive: Path, *, pack_id: str) -> None:
    archive.parent.mkdir(parents=True, exist_ok=True)
    with (
        archive.open("wb") as raw,
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
        for identity in tree_identities(pack):
            relative = safe_relative_path(identity.path)
            source = pack / relative
            info = tarfile.TarInfo(f"{_ARCHIVE_ROOT}/{pack_id}/{relative}")
            info.size = identity.size_bytes
            info.mode = 0o644
            info.mtime = 0
            info.uid = 0
            info.gid = 0
            info.uname = ""
            info.gname = ""
            with source.open("rb") as body:
                tar.addfile(info, body)


def _extract_archive(archive: Path, *, destination: Path, pack_id: str) -> None:
    root = f"{_ARCHIVE_ROOT}/{pack_id}/"
    seen: set[str] = set()
    total_bytes = 0
    unpack_root = destination / _ARCHIVE_ROOT / pack_id
    with tarfile.open(archive, mode="r:gz", encoding="utf-8", errors="strict") as tar:
        for member in tar:
            if (
                not member.isfile()
                or member.name in seen
                or not member.name.startswith(root)
                or member.mode != 0o644
                or member.mtime != 0
                or member.uid != 0
                or member.gid != 0
                or member.uname
                or member.gname
            ):
                raise CorpusError("public practice archive metadata is invalid")
            if member.size < 0 or member.size > _MAX_UNCOMPRESSED_BYTES:
                raise CorpusError(
                    "public practice archive member exceeds its byte bound"
                )
            if len(seen) >= _MAX_ARCHIVE_MEMBERS:
                raise CorpusError("public practice archive has too many members")
            remaining = _MAX_UNCOMPRESSED_BYTES - total_bytes
            if member.size > remaining:
                raise CorpusError(
                    "public practice archive exceeds its uncompressed bound"
                )
            relative = safe_relative_path(member.name.removeprefix(root))
            if Path(relative).drive or Path(relative).is_absolute():
                raise CorpusError("public practice archive member path is not relative")
            target = unpack_root / relative
            try:
                resolved_root = unpack_root.resolve()
                resolved_target = target.resolve()
            except OSError as error:
                raise CorpusError(
                    "public practice archive member path is invalid"
                ) from error
            if not resolved_target.is_relative_to(resolved_root):
                raise CorpusError("public practice archive member path is not relative")
            target.parent.mkdir(parents=True, exist_ok=True)
            source = tar.extractfile(member)
            if source is None:  # pragma: no cover - tarfile invariant
                raise CorpusError("public practice archive member is unreadable")
            with source, target.open("xb") as output:
                copied = _copy_bounded(source, output, limit=member.size)
            if copied != member.size:
                raise CorpusError("public practice archive member size is invalid")
            total_bytes += copied
            seen.add(member.name)
    if not seen:
        raise CorpusError("public practice archive is empty")


def _copy_bounded(source: Any, dest: Any, *, limit: int) -> int:
    copied = 0
    while copied < limit:
        chunk = source.read(min(1024 * 1024, limit - copied))
        if not chunk:
            break
        dest.write(chunk)
        copied += len(chunk)
    if source.read(1):
        raise CorpusError("public practice archive member exceeds its byte bound")
    return copied


def _validate_descriptor(value: dict[str, Any]) -> None:
    if frozenset(value) != _DESCRIPTOR_KEYS:
        raise CorpusError("public practice descriptor fields are invalid")
    if (
        value["schema"] != _RELEASE_SCHEMA
        or value["corpus_scope"] != "public_practice"
        or value["generation_mode"] != "static_public_protocol_demo"
        or value["weight_eligible"] is not False
        or type(value["coding_contract_version"]) is not int
        or type(value["task_count"]) is not int
        or type(value["memory_count"]) is not int
        or type(value["user_count"]) is not int
        or type(value["file_count"]) is not int
        or type(value["task_entropy_bits"]) is not int
        or type(value["archive_size_bytes"]) is not int
        or value["archive_size_bytes"] < 1
    ):
        raise CorpusError("public practice descriptor authority is invalid")
    pack_id = _string(value, "practice_pack_id")
    if _PACK_ID.fullmatch(pack_id) is None:
        raise CorpusError("public practice descriptor pack identity is invalid")
    if not value["archive_name"] == f"{pack_id}.tar.gz":
        raise CorpusError("public practice archive name is invalid")
    if (
        value["manifest_name"] != "manifest.json"
        or value["release_notes_name"] != "RELEASE.md"
    ):
        raise CorpusError("public practice release filenames are invalid")
    for field in ("archive_sha256", "manifest_sha256", "source_sha256"):
        if not _sha256(value.get(field)):
            raise CorpusError(f"public practice descriptor {field} is invalid")


def _validate_descriptor_against_manifest(
    descriptor: dict[str, Any], manifest: dict[str, Any]
) -> None:
    for field in (
        "coding_contract_version",
        "corpus_scope",
        "generation_mode",
        "memory_count",
        "practice_pack_id",
        "source_sha256",
        "task_count",
        "task_entropy_bits",
        "user_count",
        "weight_eligible",
    ):
        if descriptor[field] != manifest[field]:
            raise CorpusError("public practice descriptor disagrees with manifest")
    if descriptor["file_count"] != len(manifest["files"]):
        raise CorpusError("public practice descriptor file count disagrees")


def _release_notes(descriptor: dict[str, Any]) -> str:
    return "\n".join(
        (
            "# DittoBench Coding Public Practice Release",
            "",
            "This is a static public protocol-development pack. It is not a",
            "scored corpus and is permanently ineligible for emissions.",
            "",
            f"- Pack: `{descriptor['practice_pack_id']}`",
            f"- Manifest SHA-256: `{descriptor['manifest_sha256']}`",
            f"- Archive SHA-256: `{descriptor['archive_sha256']}`",
            f"- Tasks: `{descriptor['task_count']}`",
            "",
            "Download the archive, verify it with the release descriptor, then use",
            "the local coding datagen commands to materialize or evaluate a task.",
            "",
        )
    )


def _sha256(value: object) -> bool:
    return (
        isinstance(value, str)
        and len(value) == 64
        and all(character in "0123456789abcdef" for character in value)
    )


def _string(value: dict[str, Any], field: str) -> str:
    candidate = value.get(field)
    if not isinstance(candidate, str) or not candidate:
        raise CorpusError(f"public practice descriptor {field} is invalid")
    return candidate
