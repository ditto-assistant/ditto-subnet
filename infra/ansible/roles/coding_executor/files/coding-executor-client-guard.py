#!/usr/bin/env python3
"""Continuously prove the dedicated rootless coding image remains attested.

This is deliberately a socket-client guard, not a scorer. It uses only Docker
``info`` and ``image inspect`` against the fixed rootless Unix socket; it never
loads, pulls, creates, starts, runs, removes, or publishes an image/container.
It exposes no network or Unix listener and owns no credential or ticket.
"""

from __future__ import annotations

import argparse
import grp
import json
import os
import re
import selectors
import stat
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

MAX_DOCKER_OUTPUT = 1 << 20
CONTROL_TIMEOUT_SECONDS = 60
CHECK_INTERVAL_SECONDS = 60
ATTESTATION_SCHEMA = "dittobench-coding-runtime-image-attestation-v1"
EXPECTED_DOCKER_HOST = "unix:///run/ditto-coding-executor/docker.sock"
EXPECTED_ATTESTATION_PATH = Path(
    "/var/lib/ditto-coding-executor/attestations/runtime-image-attestation.json"
)
EXPECTED_CLIENT_GROUP = "ditto-coding-client"
ISOLATED_DAEMON_LABEL = "io.heyditto.dittobench.isolated=true"
SUPERVISOR_CONTRACT_LABEL = "io.heyditto.dittobench.coding-supervisor-contract"
FIXTURE_LABEL = "io.heyditto.dittobench.coding-supervisor-fixture"
TRUSTED_DRIVER_DIGEST_LABEL = "io.heyditto.dittobench.trusted-test-driver-sha256"
TRUSTED_DRIVER_NAME_LABEL = "io.heyditto.dittobench.trusted-test-driver-name"
SOURCE_REVISION_LABEL = "org.opencontainers.image.revision"
SUPERVISOR_ENTRYPOINT = "/usr/local/bin/dittobench-coding-supervisor"
TRUSTED_DRIVER_NAME = "dittobench-test-driver"
SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
OCI_DIGEST_RE = re.compile(r"^sha256:[0-9a-f]{64}$")
SOURCE_REVISION_RE = re.compile(r"^[0-9a-f]{40}$")
ATTESTATION_FIELDS = {
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


class GuardError(ValueError):
    """Raised when the daemon/image/attestation trust boundary has drifted."""


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--once", action="store_true")
    return parser.parse_args()


def fail(message: str) -> None:
    raise GuardError(message)


def reject_duplicate_object_keys(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    value: dict[str, Any] = {}
    for key, item in pairs:
        if key in value:
            fail(f"attestation has duplicate key: {key}")
        value[key] = item
    return value


def client_group_id() -> int:
    try:
        return grp.getgrnam(EXPECTED_CLIENT_GROUP).gr_gid
    except KeyError:
        fail("dedicated Docker client group is unavailable")


def regular_client_attestation(path: Path) -> os.stat_result:
    try:
        metadata = path.lstat()
    except OSError as exc:
        fail(f"runtime-image attestation is unavailable: {exc}")
    if not stat.S_ISREG(metadata.st_mode) or stat.S_ISLNK(metadata.st_mode):
        fail("runtime-image attestation is not a regular file")
    if (
        metadata.st_uid != 0
        or metadata.st_gid != client_group_id()
        or stat.S_IMODE(metadata.st_mode) != 0o640
    ):
        fail("runtime-image attestation ownership or mode is invalid")
    if metadata.st_size <= 0 or metadata.st_size > 16 << 10:
        fail("runtime-image attestation size is outside its bound")
    return metadata


def require_string(value: Any, label: str) -> str:
    if not isinstance(value, str):
        fail(f"runtime-image attestation {label} is not a string")
    return value


def read_attestation(path: Path) -> dict[str, str]:
    if path != EXPECTED_ATTESTATION_PATH:
        fail("runtime-image attestation path is not fixed")
    metadata = regular_client_attestation(path)
    try:
        raw = path.read_bytes()
        current = path.lstat()
    except OSError as exc:
        fail(f"runtime-image attestation cannot be read: {exc}")
    if len(raw) != metadata.st_size or (
        current.st_dev,
        current.st_ino,
        current.st_size,
        current.st_mtime_ns,
    ) != (metadata.st_dev, metadata.st_ino, metadata.st_size, metadata.st_mtime_ns):
        fail("runtime-image attestation changed while being read")
    try:
        decoded = json.loads(raw, object_pairs_hook=reject_duplicate_object_keys)
    except (json.JSONDecodeError, UnicodeDecodeError) as exc:
        fail(f"runtime-image attestation is not strict JSON: {exc}")
    if not isinstance(decoded, dict) or set(decoded) != ATTESTATION_FIELDS:
        fail("runtime-image attestation fields are invalid")
    value = {key: require_string(item, key) for key, item in decoded.items()}
    if value["schema"] != ATTESTATION_SCHEMA or value["platform"] != "linux/amd64":
        fail("runtime-image attestation schema or platform is invalid")
    if value["supervisor_contract"] != "1":
        fail("runtime-image attestation supervisor contract is invalid")
    if not SOURCE_REVISION_RE.fullmatch(value["source_revision"]):
        fail("runtime-image attestation source revision is invalid")
    for field in ("archive_sha256", "manifest_sha256"):
        if not SHA256_RE.fullmatch(value[field]):
            fail(f"runtime-image attestation {field} is invalid")
    if not OCI_DIGEST_RE.fullmatch(value["image_id"]):
        fail("runtime-image attestation image ID is invalid")
    if not OCI_DIGEST_RE.fullmatch(value["trusted_test_driver_digest"]):
        fail("runtime-image attestation trusted-driver digest is invalid")
    repository, separator, digest = value["image_reference"].partition("@")
    if not repository or separator != "@" or not OCI_DIGEST_RE.fullmatch(digest):
        fail("runtime-image attestation image reference is invalid")
    return value


def terminate(process: subprocess.Popen[bytes]) -> None:
    process.terminate()
    try:
        process.wait(timeout=5)
    except subprocess.TimeoutExpired:
        process.kill()
        process.wait()


def docker_output(arguments: list[str]) -> bytes:
    try:
        process = subprocess.Popen(
            ["/usr/bin/docker", "--host", EXPECTED_DOCKER_HOST, *arguments],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            close_fds=True,
            cwd="/",
            env={"HOME": "/nonexistent", "LANG": "C", "PATH": "/usr/bin:/bin"},
        )
    except OSError as exc:
        fail(f"dedicated Docker control command cannot start: {exc}")
    assert process.stdout is not None
    output = bytearray()
    deadline = time.monotonic() + CONTROL_TIMEOUT_SECONDS
    selector = selectors.DefaultSelector()
    selector.register(process.stdout, selectors.EVENT_READ)
    try:
        while selector.get_map():
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                terminate(process)
                fail("dedicated Docker control command timed out")
            for key, _ in selector.select(remaining):
                chunk = os.read(key.fd, min(65536, MAX_DOCKER_OUTPUT + 1 - len(output)))
                if not chunk:
                    selector.unregister(key.fileobj)
                    continue
                output.extend(chunk)
                if len(output) > MAX_DOCKER_OUTPUT:
                    terminate(process)
                    fail("dedicated Docker control output exceeded its bound")
        returncode = process.wait(timeout=max(1, deadline - time.monotonic()))
    except subprocess.TimeoutExpired:
        terminate(process)
        fail("dedicated Docker control command timed out")
    finally:
        selector.close()
    if returncode != 0:
        fail("dedicated Docker control command failed")
    return bytes(output)


def json_value(raw: bytes, label: str) -> Any:
    try:
        return json.loads(raw)
    except (json.JSONDecodeError, UnicodeDecodeError) as exc:
        fail(f"dedicated Docker {label} output is not JSON: {exc}")


def daemon_has_label(value: Any, expected: str) -> bool:
    if isinstance(value, list):
        return expected in value
    if isinstance(value, dict):
        key, separator, expected_value = expected.partition("=")
        return separator == "=" and value.get(key) == expected_value
    return False


def credential_image_environment(value: str) -> bool:
    name = value.partition("=")[0].upper()
    if name in {"HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY"}:
        return True
    return any(
        marker in name
        for marker in ("KEY", "TOKEN", "SECRET", "PASSWORD", "CREDENTIAL")
    )


def inspect_daemon() -> None:
    security = json_value(
        docker_output(["info", "--format", "{{json .SecurityOptions}}"]),
        "security options",
    )
    if not isinstance(security, list) or not any(
        isinstance(value, str)
        and (
            value.lower() == "rootless"
            or value.lower() == "name=rootless"
            or value.lower().startswith("name=rootless,")
        )
        for value in security
    ):
        fail("dedicated Docker daemon is not rootless")
    labels = json_value(
        docker_output(["info", "--format", "{{json .Labels}}"]),
        "labels",
    )
    if not daemon_has_label(labels, ISOLATED_DAEMON_LABEL):
        fail("dedicated Docker daemon lacks the isolated label")


def inspect_image(attestation: dict[str, str]) -> None:
    raw = docker_output(
        ["image", "inspect", "--format", "{{json .}}", attestation["image_reference"]]
    )
    image = json_value(raw, "image inspection")
    if not isinstance(image, dict) or image.get("Id") != attestation["image_id"]:
        fail("dedicated Docker image ID does not match the attestation")
    if image.get("Os") != "linux" or image.get("Architecture") != "amd64":
        fail("dedicated Docker image platform does not match the attestation")
    repo_digests = image.get("RepoDigests")
    if (
        not isinstance(repo_digests, list)
        or attestation["image_reference"] not in repo_digests
    ):
        fail("dedicated Docker image repository digest does not match the attestation")
    config = image.get("Config")
    if not isinstance(config, dict) or config.get("Volumes") not in (None, {}):
        fail("dedicated Docker image config is invalid")
    labels = config.get("Labels")
    if not isinstance(labels, dict) or not all(
        isinstance(key, str) and isinstance(value, str) for key, value in labels.items()
    ):
        fail("dedicated Docker image labels are invalid")
    if labels.get(SUPERVISOR_CONTRACT_LABEL) != attestation["supervisor_contract"]:
        fail("dedicated Docker image supervisor contract drifted")
    if labels.get(FIXTURE_LABEL) == "true":
        fail("dedicated Docker image is the public certification fixture")
    if (
        labels.get(TRUSTED_DRIVER_DIGEST_LABEL)
        != attestation["trusted_test_driver_digest"]
    ):
        fail("dedicated Docker image trusted-driver digest drifted")
    if labels.get(TRUSTED_DRIVER_NAME_LABEL) != TRUSTED_DRIVER_NAME:
        fail("dedicated Docker image trusted-driver name drifted")
    if labels.get(SOURCE_REVISION_LABEL) != attestation["source_revision"]:
        fail("dedicated Docker image source revision drifted")
    environment = config.get("Env")
    if not isinstance(environment, list) or not all(
        isinstance(value, str) for value in environment
    ):
        fail("dedicated Docker image environment is invalid")
    if any(credential_image_environment(value) for value in environment):
        fail("dedicated Docker image has credential-shaped environment")
    if config.get("Entrypoint") != [SUPERVISOR_ENTRYPOINT]:
        fail("dedicated Docker image supervisor entrypoint drifted")


def guard_once() -> None:
    if os.geteuid() == 0:
        fail("dedicated client guard must not run as root")
    group_id = client_group_id()
    if group_id not in {os.getgid(), *os.getgroups()}:
        fail("dedicated client guard lacks Docker client-group membership")
    attestation = read_attestation(EXPECTED_ATTESTATION_PATH)
    inspect_daemon()
    inspect_image(attestation)


def main() -> int:
    args = parse_args()
    if args.once:
        guard_once()
        return 0
    while True:
        guard_once()
        time.sleep(CHECK_INTERVAL_SECONDS)


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except GuardError:
        print("dedicated coding client guard refused the runtime", file=sys.stderr)
        raise SystemExit(1) from None
