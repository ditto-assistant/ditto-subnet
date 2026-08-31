#!/usr/bin/env python3
"""Verify an already-staged production runtime bundle without consuming it.

The complete raw manifest SHA-256 is pinned in protected host configuration.
The manifest in turn binds the source revision, fixed contract/platform, exact
image and trusted-driver digests, and the archive bytes. This deliberately does
not inspect a Docker daemon or load an image: a later reviewed role must do so
only after it can prove the loaded image is the non-fixture production image.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import stat
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any

MAX_MANIFEST_BYTES = 32 * 1024
MAX_ARCHIVE_BYTES = 8 * 1024 * 1024 * 1024
SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
OCI_DIGEST_RE = re.compile(r"^sha256:[0-9a-f]{64}$")
SOURCE_REVISION_RE = re.compile(r"^[0-9a-f]{40}$")
IMAGE_REPOSITORY_RE = re.compile(r"^[a-z0-9][a-z0-9._/-]*$")
EXPECTED_FIELDS = {
    "archive_sha256",
    "fixture",
    "image_digest",
    "image_repository",
    "platform",
    "schema",
    "source_revision",
    "supervisor_contract",
    "trusted_test_driver_digest",
}


class VerificationError(ValueError):
    """Raised for any malformed or untrusted staging input."""


@dataclass(frozen=True)
class VerifiedRuntimeBundle:
    """The immutable manifest and archive identities proved by this verifier."""

    manifest: dict[str, Any]
    manifest_sha256: str
    archive_sha256: str


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--manifest", required=True, type=Path)
    parser.add_argument("--archive", required=True, type=Path)
    parser.add_argument("--expected-manifest-sha256", required=True)
    return parser.parse_args()


def fail(message: str) -> None:
    raise VerificationError(message)


def regular_root_owned_file(path: Path, *, maximum: int) -> os.stat_result:
    try:
        metadata = path.lstat()
    except FileNotFoundError:
        fail(f"required staged file is absent: {path}")
    if not stat.S_ISREG(metadata.st_mode) or stat.S_ISLNK(metadata.st_mode):
        fail(f"staged path is not a regular file: {path}")
    if metadata.st_uid != 0 or metadata.st_gid != 0:
        fail(f"staged file is not owned by root: {path}")
    if metadata.st_mode & 0o077:
        fail(f"staged file is accessible outside root: {path}")
    if metadata.st_size <= 0 or metadata.st_size > maximum:
        fail(f"staged file size is outside its bound: {path}")
    return metadata


def sha256_file(path: Path, *, maximum: int) -> str:
    metadata = regular_root_owned_file(path, maximum=maximum)
    digest = hashlib.sha256()
    remaining = metadata.st_size
    try:
        with path.open("rb", buffering=0) as source:
            while remaining:
                block = source.read(min(1024 * 1024, remaining))
                if not block:
                    fail(f"staged file changed while being hashed: {path}")
                digest.update(block)
                remaining -= len(block)
        current = path.lstat()
    except OSError as exc:
        fail(f"cannot read staged file: {exc}")
    if (current.st_dev, current.st_ino, current.st_size, current.st_mtime_ns) != (
        metadata.st_dev,
        metadata.st_ino,
        metadata.st_size,
        metadata.st_mtime_ns,
    ):
        fail(f"staged file changed while being verified: {path}")
    return digest.hexdigest()


def reject_duplicate_object_keys(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    value: dict[str, Any] = {}
    for key, item in pairs:
        if key in value:
            fail(f"manifest has duplicate key: {key}")
        value[key] = item
    return value


def read_manifest(path: Path) -> tuple[dict[str, Any], str]:
    metadata = regular_root_owned_file(path, maximum=MAX_MANIFEST_BYTES)
    try:
        raw = path.read_bytes()
        current = path.lstat()
    except OSError as exc:
        fail(f"cannot read staged manifest: {exc}")
    if len(raw) != metadata.st_size or (
        current.st_dev,
        current.st_ino,
        current.st_size,
        current.st_mtime_ns,
    ) != (metadata.st_dev, metadata.st_ino, metadata.st_size, metadata.st_mtime_ns):
        fail("manifest changed while being verified")
    try:
        decoded = json.loads(raw, object_pairs_hook=reject_duplicate_object_keys)
    except (json.JSONDecodeError, UnicodeDecodeError) as exc:
        fail(f"manifest is not strict JSON: {exc}")
    if not isinstance(decoded, dict) or set(decoded) != EXPECTED_FIELDS:
        fail("manifest fields do not exactly match the runtime-bundle schema")
    return decoded, hashlib.sha256(raw).hexdigest()


def require_string(manifest: dict[str, Any], field: str) -> str:
    value = manifest[field]
    if not isinstance(value, str):
        fail(f"manifest {field} must be a string")
    return value


def validate_manifest(manifest: dict[str, Any], archive_sha256: str) -> None:
    if manifest["schema"] != "dittobench-coding-runtime-manifest-v1":
        fail("manifest schema is not supported")
    if manifest["platform"] != "linux/amd64":
        fail("manifest platform is not linux/amd64")
    if manifest["supervisor_contract"] != "1":
        fail("manifest supervisor contract is not 1")
    if manifest["fixture"] is not False:
        fail("manifest must explicitly reject the certification fixture")
    if not SOURCE_REVISION_RE.fullmatch(require_string(manifest, "source_revision")):
        fail("manifest source revision is not a full lowercase SHA-1")
    image_repository = require_string(manifest, "image_repository")
    if (
        not IMAGE_REPOSITORY_RE.fullmatch(image_repository)
        or "//" in image_repository
        or any(part in {"", ".", ".."} for part in image_repository.split("/"))
    ):
        fail("manifest image repository is not a normalized repository path")
    for field in ("image_digest", "trusted_test_driver_digest"):
        if not OCI_DIGEST_RE.fullmatch(require_string(manifest, field)):
            fail(f"manifest {field} is not a lowercase sha256 OCI digest")
    declared_archive_sha256 = require_string(manifest, "archive_sha256")
    if not SHA256_RE.fullmatch(declared_archive_sha256):
        fail("manifest archive SHA-256 is not lowercase hexadecimal")
    if declared_archive_sha256 != archive_sha256:
        fail("staged archive SHA-256 does not match the manifest")


def verify_runtime_bundle(
    manifest_path: Path,
    archive_path: Path,
    expected_manifest_sha256: str,
) -> VerifiedRuntimeBundle:
    if not SHA256_RE.fullmatch(expected_manifest_sha256):
        fail("expected manifest SHA-256 is not lowercase hexadecimal")
    manifest, manifest_sha256 = read_manifest(manifest_path)
    if manifest_sha256 != expected_manifest_sha256:
        fail("staged manifest SHA-256 does not match protected host configuration")
    archive_sha256 = sha256_file(archive_path, maximum=MAX_ARCHIVE_BYTES)
    validate_manifest(manifest, archive_sha256)
    return VerifiedRuntimeBundle(
        manifest=manifest,
        manifest_sha256=manifest_sha256,
        archive_sha256=archive_sha256,
    )


def main() -> int:
    args = parse_args()
    verify_runtime_bundle(
        args.manifest,
        args.archive,
        args.expected_manifest_sha256,
    )
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except VerificationError as exc:
        print(f"runtime-bundle verification failed: {exc}", file=sys.stderr)
        raise SystemExit(1) from None
