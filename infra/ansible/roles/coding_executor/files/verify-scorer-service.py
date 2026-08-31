#!/usr/bin/env python3
"""Verify the fixed attested scorer host runtime before systemd execs it."""

from __future__ import annotations

import grp
import hashlib
import ipaddress
import json
import os
import stat
import sys
from pathlib import Path
from typing import Any

CLIENT_GROUP = "ditto-coding-client"
RUNTIME_BINARY = Path(
    "/usr/local/lib/ditto-coding-executor/dittobench-coding-executor-scorer"
)
RUNTIME_POLICY = Path("/opt/ditto/coding/coding_inference_policy_locked_v1.json")
RUNTIME_ATTESTATION = Path(
    "/var/lib/ditto-coding-executor/attestations/scorer-runtime-attestation.json"
)
SCORER_ATTESTATION = Path(
    "/var/lib/ditto-coding-executor/attestations/scorer-image-attestation.json"
)
IMAGE_ATTESTATION = Path(
    "/var/lib/ditto-coding-executor/attestations/runtime-image-attestation.json"
)
DOCKER_HOST = "unix:///run/ditto-coding-executor/docker.sock"
LOCKED_POLICY_FILE_SHA256 = (
    "6dd79225817b56ebf155f8344cd5faf752c8dd57802b21d6d2cbbae9cc2ff0b4"
)
SHA256_RE = __import__("re").compile(r"^[0-9a-f]{64}$")
OCI_RE = __import__("re").compile(r"^sha256:[0-9a-f]{64}$")
SOURCE_RE = __import__("re").compile(r"^[0-9a-f]{40}$")
RUNTIME_FIELDS = {
    "binary_sha256",
    "locked_policy_sha256",
    "policy_file_sha256",
    "schema",
    "scorer_attestation_sha256",
    "scorer_contract",
    "scorer_image_id",
    "scorer_image_reference",
    "source_revision",
}
SCORER_FIELDS = {
    "archive_sha256",
    "bundle_manifest_sha256",
    "image_id",
    "image_reference",
    "locked_policy_sha256",
    "platform",
    "release_manifest_sha256",
    "schema",
    "scorer_contract",
    "source_revision",
}
IMAGE_FIELDS = {
    "archive_sha256",
    "image_id",
    "image_reference",
    "manifest_sha256",
    "platform",
    "schema",
    "source_revision",
    "supervisor_contract",
    "trusted_test_driver_digest",
}


class VerificationError(ValueError):
    pass


def fail(message: str) -> None:
    raise VerificationError(message)


def client_gid() -> int:
    try:
        return grp.getgrnam(CLIENT_GROUP).gr_gid
    except KeyError:
        fail("dedicated Docker client group is unavailable")


def reject_duplicates(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            fail("attestation has duplicate field")
        result[key] = value
    return result


def read_client_json(path: Path, fields: set[str]) -> tuple[dict[str, str], str]:
    try:
        metadata = path.lstat()
        raw = path.read_bytes()
    except OSError as exc:
        fail(f"attestation is unavailable: {exc}")
    if (
        not stat.S_ISREG(metadata.st_mode)
        or stat.S_ISLNK(metadata.st_mode)
        or metadata.st_uid != 0
        or metadata.st_gid != client_gid()
        or stat.S_IMODE(metadata.st_mode) != 0o640
        or len(raw) != metadata.st_size
        or not 0 < len(raw) <= 16 << 10
    ):
        fail("attestation ownership or size is invalid")
    try:
        decoded = json.loads(raw, object_pairs_hook=reject_duplicates)
    except (json.JSONDecodeError, UnicodeDecodeError) as exc:
        fail(f"attestation is not strict JSON: {exc}")
    if not isinstance(decoded, dict) or set(decoded) != fields:
        fail("attestation fields are invalid")
    if not all(isinstance(value, str) for value in decoded.values()):
        fail("attestation values are invalid")
    return decoded, hashlib.sha256(raw).hexdigest()  # type: ignore[return-value]


def sha256_file(path: Path, *, mode: int, maximum: int) -> str:
    try:
        metadata = path.lstat()
    except OSError as exc:
        fail(f"runtime file is unavailable: {exc}")
    if (
        not stat.S_ISREG(metadata.st_mode)
        or stat.S_ISLNK(metadata.st_mode)
        or metadata.st_uid != 0
        or metadata.st_gid != 0
        or stat.S_IMODE(metadata.st_mode) != mode
        or not 0 < metadata.st_size <= maximum
    ):
        fail("runtime file ownership or size is invalid")
    digest = hashlib.sha256()
    with path.open("rb", buffering=0) as source:
        while block := source.read(1 << 20):
            digest.update(block)
    return digest.hexdigest()


def runtime_repository(image_attestation: dict[str, str]) -> str:
    repository, separator, digest = image_attestation["image_reference"].partition("@")
    if not repository or separator != "@" or not OCI_RE.fullmatch(digest):
        fail("runtime image reference is invalid")
    return repository


def verify_service() -> str:
    if os.geteuid() == 0 or client_gid() not in {os.getgid(), *os.getgroups()}:
        fail("scorer service identity is invalid")
    if os.environ.get("DOCKER_HOST") != DOCKER_HOST:
        fail("scorer service Docker host is invalid")
    try:
        gateway = ipaddress.ip_address(
            os.environ.get("DITTOBENCH_SANDBOX_HOST_GATEWAY_IP", "")
        )
    except ValueError:
        fail("scorer capability gateway is invalid")
    if gateway.version != 4 or gateway.is_loopback or gateway.is_unspecified:
        fail("scorer capability gateway is invalid")
    credentials = Path(os.environ.get("CREDENTIALS_DIRECTORY", ""))
    token = credentials / "control-token"
    if not credentials.is_absolute() or not str(credentials).startswith(
        "/run/credentials/"
    ):
        fail("scorer credentials directory is invalid")
    try:
        token_metadata = token.lstat()
        token_value = token.read_bytes().strip()
    except OSError as exc:
        fail(f"scorer credential is unavailable: {exc}")
    if (
        not stat.S_ISREG(token_metadata.st_mode)
        or stat.S_ISLNK(token_metadata.st_mode)
        or len(token_value) < 32
    ):
        fail("scorer credential is invalid")
    runtime, _ = read_client_json(RUNTIME_ATTESTATION, RUNTIME_FIELDS)
    scorer, scorer_sha256 = read_client_json(SCORER_ATTESTATION, SCORER_FIELDS)
    image, _ = read_client_json(IMAGE_ATTESTATION, IMAGE_FIELDS)
    if (
        runtime["schema"] != "dittobench-coding-executor-scorer-runtime-v1"
        or runtime["scorer_contract"] != "1"
        or runtime["scorer_attestation_sha256"] != scorer_sha256
        or runtime["scorer_image_id"] != scorer["image_id"]
        or runtime["scorer_image_reference"] != scorer["image_reference"]
        or runtime["locked_policy_sha256"] != scorer["locked_policy_sha256"]
        or runtime["source_revision"] != scorer["source_revision"]
        or image["schema"] != "dittobench-coding-runtime-image-attestation-v1"
        or image["platform"] != "linux/amd64"
    ):
        fail("scorer runtime attestation chain is invalid")
    for field in (
        "binary_sha256",
        "locked_policy_sha256",
        "policy_file_sha256",
        "scorer_attestation_sha256",
    ):
        if not SHA256_RE.fullmatch(runtime[field]):
            fail("scorer runtime digest is invalid")
    if not OCI_RE.fullmatch(runtime["scorer_image_id"]) or not SOURCE_RE.fullmatch(
        runtime["source_revision"]
    ):
        fail("scorer runtime identity is invalid")
    if (
        sha256_file(RUNTIME_BINARY, mode=0o755, maximum=256 << 20)
        != runtime["binary_sha256"]
    ):
        fail("scorer binary drifted")
    if (
        sha256_file(RUNTIME_POLICY, mode=0o444, maximum=64 << 10)
        != runtime["policy_file_sha256"]
    ):
        fail("scorer policy drifted")
    if runtime["policy_file_sha256"] != LOCKED_POLICY_FILE_SHA256:
        fail("scorer policy file is not locked")
    return runtime_repository(image)


def main() -> int:
    verify_service()
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except VerificationError:
        print("attested scorer service preflight refused runtime", file=sys.stderr)
        raise SystemExit(1) from None
