#!/usr/bin/env python3
"""Verify one root-owned scorer OCI bundle without loading it."""

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
EXPECTED_IMAGE_REPOSITORY = "ghcr.io/ditto-assistant/dittobench-coding-executor-scorer"
RELEASE_FIELDS = {
    "image_digest",
    "image_reference",
    "locked_policy_sha256",
    "platform",
    "schema",
    "scorer_contract",
    "source_revision",
}
BUNDLE_FIELDS = RELEASE_FIELDS | {
    "archive_sha256",
    "image_id",
    "release_manifest_sha256",
}


class VerificationError(ValueError):
    """Raised for any malformed or untrusted scorer staging input."""


@dataclass(frozen=True)
class VerifiedScorerBundle:
    """The exact scorer release, bundle, and archive identities."""

    release_manifest: dict[str, Any]
    bundle_manifest: dict[str, Any]
    release_manifest_sha256: str
    bundle_manifest_sha256: str
    archive_sha256: str


def fail(message: str) -> None:
    raise VerificationError(message)


def regular_root_owned_file(path: Path, *, maximum: int) -> os.stat_result:
    try:
        metadata = path.lstat()
    except OSError as exc:
        fail(f"cannot inspect scorer staging file: {exc}")
    if not stat.S_ISREG(metadata.st_mode) or stat.S_ISLNK(metadata.st_mode):
        fail(f"scorer staging path is not a regular file: {path}")
    if metadata.st_uid != 0 or metadata.st_gid != 0:
        fail(f"scorer staging file is not owned by root: {path}")
    if metadata.st_mode & 0o077:
        fail(f"scorer staging file is accessible outside root: {path}")
    if metadata.st_size <= 0 or metadata.st_size > maximum:
        fail(f"scorer staging file size is outside its bound: {path}")
    return metadata


def stable_read(path: Path, *, maximum: int) -> bytes:
    metadata = regular_root_owned_file(path, maximum=maximum)
    try:
        raw = path.read_bytes()
        current = path.lstat()
    except OSError as exc:
        fail(f"cannot read scorer staging file: {exc}")
    if len(raw) != metadata.st_size or (
        current.st_dev,
        current.st_ino,
        current.st_size,
        current.st_mtime_ns,
    ) != (
        metadata.st_dev,
        metadata.st_ino,
        metadata.st_size,
        metadata.st_mtime_ns,
    ):
        fail(f"scorer staging file changed while being read: {path}")
    return raw


def sha256_file(path: Path, *, maximum: int) -> str:
    metadata = regular_root_owned_file(path, maximum=maximum)
    digest = hashlib.sha256()
    remaining = metadata.st_size
    try:
        with path.open("rb", buffering=0) as source:
            while remaining:
                block = source.read(min(1 << 20, remaining))
                if not block:
                    fail(f"scorer staging file changed while being hashed: {path}")
                digest.update(block)
                remaining -= len(block)
        current = path.lstat()
    except OSError as exc:
        fail(f"cannot hash scorer staging file: {exc}")
    if (
        current.st_dev,
        current.st_ino,
        current.st_size,
        current.st_mtime_ns,
    ) != (
        metadata.st_dev,
        metadata.st_ino,
        metadata.st_size,
        metadata.st_mtime_ns,
    ):
        fail(f"scorer staging file changed while being verified: {path}")
    return digest.hexdigest()


def reject_duplicate_object_keys(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    value: dict[str, Any] = {}
    for key, item in pairs:
        if key in value:
            fail(f"scorer manifest has duplicate key: {key}")
        value[key] = item
    return value


def decode_document(raw: bytes, fields: set[str], label: str) -> dict[str, Any]:
    try:
        value = json.loads(raw, object_pairs_hook=reject_duplicate_object_keys)
    except (json.JSONDecodeError, UnicodeDecodeError) as exc:
        fail(f"{label} is not strict JSON: {exc}")
    if not isinstance(value, dict) or set(value) != fields:
        fail(f"{label} fields do not exactly match the schema")
    return value


def require_string(document: dict[str, Any], field: str, label: str) -> str:
    value = document[field]
    if not isinstance(value, str):
        fail(f"{label} {field} must be a string")
    return value


def validate_release(release: dict[str, Any]) -> None:
    if release["schema"] != "dittobench-coding-executor-scorer-release-v1":
        fail("release manifest schema is invalid")
    if release["platform"] != "linux/amd64" or release["scorer_contract"] != "1":
        fail("scorer platform or contract is invalid")
    if not SOURCE_REVISION_RE.fullmatch(
        require_string(release, "source_revision", "release manifest")
    ):
        fail("release manifest source revision is invalid")
    image_digest = require_string(release, "image_digest", "release manifest")
    if not OCI_DIGEST_RE.fullmatch(image_digest):
        fail("image digest is invalid")
    if not SHA256_RE.fullmatch(
        require_string(release, "locked_policy_sha256", "release manifest")
    ):
        fail("locked policy digest is invalid")
    image_reference = require_string(release, "image_reference", "release manifest")
    repository, separator, reference_digest = image_reference.partition("@")
    if (
        separator != "@"
        or repository != EXPECTED_IMAGE_REPOSITORY
        or not IMAGE_REPOSITORY_RE.fullmatch(repository)
        or "//" in repository
        or reference_digest != image_digest
    ):
        fail("scorer image reference is not the exact release repository digest")


def validate_documents(
    release_raw: bytes,
    bundle_raw: bytes,
    archive_sha256: str,
    expected_bundle_sha256: str,
) -> tuple[dict[str, Any], dict[str, Any]]:
    if not SHA256_RE.fullmatch(expected_bundle_sha256):
        fail("expected bundle manifest SHA-256 is invalid")
    bundle_sha256 = hashlib.sha256(bundle_raw).hexdigest()
    if bundle_sha256 != expected_bundle_sha256:
        fail("bundle manifest does not match protected host configuration")
    release = decode_document(release_raw, RELEASE_FIELDS, "release manifest")
    bundle = decode_document(bundle_raw, BUNDLE_FIELDS, "bundle manifest")
    validate_release(release)
    if bundle["schema"] != "dittobench-coding-executor-scorer-bundle-v1":
        fail("bundle manifest schema is invalid")
    if bundle["release_manifest_sha256"] != hashlib.sha256(release_raw).hexdigest():
        fail("release manifest digest does not match the bundle")
    if bundle["archive_sha256"] != archive_sha256:
        fail("archive digest does not match the bundle")
    if not OCI_DIGEST_RE.fullmatch(
        require_string(bundle, "image_id", "bundle manifest")
    ):
        fail("bundle manifest image ID is invalid")
    for field in RELEASE_FIELDS - {"schema"}:
        if bundle[field] != release[field]:
            fail(f"{field} differs across scorer manifests")
    return release, bundle


def verify_scorer_bundle(
    release_manifest_path: Path,
    bundle_manifest_path: Path,
    archive_path: Path,
    expected_bundle_sha256: str,
) -> VerifiedScorerBundle:
    release_raw = stable_read(release_manifest_path, maximum=MAX_MANIFEST_BYTES)
    bundle_raw = stable_read(bundle_manifest_path, maximum=MAX_MANIFEST_BYTES)
    archive_sha256 = sha256_file(archive_path, maximum=MAX_ARCHIVE_BYTES)
    release, bundle = validate_documents(
        release_raw,
        bundle_raw,
        archive_sha256,
        expected_bundle_sha256,
    )
    return VerifiedScorerBundle(
        release_manifest=release,
        bundle_manifest=bundle,
        release_manifest_sha256=hashlib.sha256(release_raw).hexdigest(),
        bundle_manifest_sha256=hashlib.sha256(bundle_raw).hexdigest(),
        archive_sha256=archive_sha256,
    )


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--release-manifest", required=True, type=Path)
    parser.add_argument("--bundle-manifest", required=True, type=Path)
    parser.add_argument("--archive", required=True, type=Path)
    parser.add_argument("--expected-bundle-sha256", required=True)
    args = parser.parse_args()
    verify_scorer_bundle(
        args.release_manifest,
        args.bundle_manifest,
        args.archive,
        args.expected_bundle_sha256,
    )
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except VerificationError as exc:
        print(f"scorer bundle verification failed: {exc}", file=sys.stderr)
        raise SystemExit(1) from None
