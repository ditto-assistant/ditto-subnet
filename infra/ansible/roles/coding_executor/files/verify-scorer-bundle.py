#!/usr/bin/env python3
"""Verify one root-owned scorer OCI bundle without loading it."""

from __future__ import annotations

import argparse
import hashlib
import json
import re
from pathlib import Path

SHA256 = re.compile(r"^[0-9a-f]{64}$")


class VerificationError(ValueError):
    pass


def require_hash(value: object, label: str) -> str:
    if not isinstance(value, str) or not SHA256.fullmatch(
        value.removeprefix("sha256:")
    ):
        raise VerificationError(f"{label} is invalid")
    return value


def validate_documents(
    release_raw: bytes,
    bundle_raw: bytes,
    archive_sha256: str,
    expected_bundle_sha256: str,
) -> None:
    if hashlib.sha256(bundle_raw).hexdigest() != expected_bundle_sha256:
        raise VerificationError(
            "bundle manifest does not match protected host configuration"
        )
    release = json.loads(release_raw)
    bundle = json.loads(bundle_raw)
    if release.get("schema") != "dittobench-coding-executor-scorer-release-v1":
        raise VerificationError("release manifest schema is invalid")
    if bundle.get("schema") != "dittobench-coding-executor-scorer-bundle-v1":
        raise VerificationError("bundle manifest schema is invalid")
    if bundle.get("release_manifest_sha256") != hashlib.sha256(release_raw).hexdigest():
        raise VerificationError("release manifest digest does not match the bundle")
    if bundle.get("archive_sha256") != archive_sha256:
        raise VerificationError("archive digest does not match the bundle")
    for field in (
        "image_digest",
        "image_reference",
        "locked_policy_sha256",
        "platform",
        "scorer_contract",
        "source_revision",
    ):
        if bundle.get(field) != release.get(field):
            raise VerificationError(f"{field} differs across scorer manifests")
    if (
        release.get("platform") != "linux/amd64"
        or release.get("scorer_contract") != "1"
    ):
        raise VerificationError("scorer platform or contract is invalid")
    require_hash(release.get("image_digest"), "image digest")
    require_hash(release.get("locked_policy_sha256"), "locked policy digest")


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb", buffering=0) as source:
        while block := source.read(1 << 20):
            digest.update(block)
    return digest.hexdigest()


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--release-manifest", required=True, type=Path)
    parser.add_argument("--bundle-manifest", required=True, type=Path)
    parser.add_argument("--archive", required=True, type=Path)
    parser.add_argument("--expected-bundle-sha256", required=True)
    args = parser.parse_args()
    for path in (args.release_manifest, args.bundle_manifest, args.archive):
        metadata = path.lstat()
        if (
            not path.is_file()
            or path.is_symlink()
            or metadata.st_uid != 0
            or metadata.st_gid != 0
            or metadata.st_mode & 0o077
        ):
            raise VerificationError("scorer staging file ownership or mode is invalid")
    release_raw = args.release_manifest.read_bytes()
    bundle_raw = args.bundle_manifest.read_bytes()
    archive_digest = sha256_file(args.archive)
    validate_documents(
        release_raw, bundle_raw, archive_digest, args.expected_bundle_sha256
    )
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (OSError, json.JSONDecodeError, VerificationError) as exc:
        print(f"scorer bundle verification failed: {exc}")
        raise SystemExit(1) from None
