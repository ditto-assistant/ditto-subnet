#!/usr/bin/env python3
"""Materialize one attested scorer binary and policy without starting it.

The scorer image remains the immutable distribution authority. This root-only
step creates a stopped temporary container solely to copy its fixed binary and
policy into root-owned host paths, then removes that container. It never starts
the scorer, creates a listener, reads a token, installs a service, or changes
the dedicated Docker client group.
"""

from __future__ import annotations

import grp
import hashlib
import json
import os
import re
import selectors
import shutil
import stat
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from typing import Any

MAX_DOCKER_OUTPUT = 1 << 20
CONTROL_TIMEOUT_SECONDS = 60
ATTESTATION_SCHEMA = "dittobench-coding-executor-scorer-image-attestation-v1"
RUNTIME_SCHEMA = "dittobench-coding-executor-scorer-runtime-v1"
EXPECTED_DOCKER_HOST = "unix:///run/ditto-coding-executor/docker.sock"
EXPECTED_CLIENT_GROUP = "ditto-coding-client"
EXPECTED_IMAGE_REPOSITORY = "ghcr.io/ditto-assistant/dittobench-coding-executor-scorer"
EXPECTED_SCORER_ATTESTATION_PATH = Path(
    "/var/lib/ditto-coding-executor/attestations/scorer-image-attestation.json"
)
EXPECTED_RUNTIME_ATTESTATION_PATH = Path(
    "/var/lib/ditto-coding-executor/attestations/scorer-runtime-attestation.json"
)
EXPECTED_RUNTIME_BINARY_PATH = Path(
    "/usr/local/lib/ditto-coding-executor/dittobench-coding-executor-scorer"
)
EXPECTED_RUNTIME_POLICY_PATH = Path(
    "/opt/ditto/coding/coding_inference_policy_locked_v1.json"
)
SCORER_BINARY_NAME = "dittobench-coding-executor-scorer"
SCORER_IMAGE_BINARY_PATH = "/dittobench-coding-executor-scorer"
SCORER_IMAGE_POLICY_PATH = "/opt/ditto/coding/coding_inference_policy_locked_v1.json"
SCORER_CONTRACT_LABEL = "io.heyditto.dittobench.coding-executor-scorer-contract"
SCORER_POLICY_LABEL = "io.heyditto.dittobench.coding-executor-locked-policy-sha256"
SOURCE_REVISION_LABEL = "org.opencontainers.image.revision"
SCORER_ENTRYPOINT = "/dittobench-coding-executor-scorer"
SCORER_USER = "65532:65532"
ISOLATED_DAEMON_LABEL = "io.heyditto.dittobench.isolated=true"
LOCKED_POLICY_FILE_SHA256 = (
    "6dd79225817b56ebf155f8344cd5faf752c8dd57802b21d6d2cbbae9cc2ff0b4"
)
SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
OCI_DIGEST_RE = re.compile(r"^sha256:[0-9a-f]{64}$")
SOURCE_REVISION_RE = re.compile(r"^[0-9a-f]{40}$")
CONTAINER_ID_RE = re.compile(r"^[0-9a-f]{64}$")
SCORER_ATTESTATION_FIELDS = {
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


class MaterializationError(ValueError):
    """Raised when a scorer runtime identity cannot be proved."""


def fail(message: str) -> None:
    raise MaterializationError(message)


def client_group_id() -> int:
    try:
        return grp.getgrnam(EXPECTED_CLIENT_GROUP).gr_gid
    except KeyError:
        fail("dedicated Docker client group is unavailable")


def terminate(process: subprocess.Popen[bytes]) -> None:
    process.terminate()
    try:
        process.wait(timeout=5)
    except subprocess.TimeoutExpired:
        process.kill()
        process.wait()


def docker_output(arguments: list[str], *, timeout: int) -> bytes:
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
        fail(f"cannot start Docker control command: {exc}")
    assert process.stdout is not None
    output = bytearray()
    deadline = time.monotonic() + timeout
    selector = selectors.DefaultSelector()
    selector.register(process.stdout, selectors.EVENT_READ)
    try:
        while selector.get_map():
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                terminate(process)
                fail("Docker control command exceeded its timeout")
            for key, _ in selector.select(remaining):
                chunk = os.read(key.fd, min(65536, MAX_DOCKER_OUTPUT + 1 - len(output)))
                if not chunk:
                    selector.unregister(key.fileobj)
                    continue
                output.extend(chunk)
                if len(output) > MAX_DOCKER_OUTPUT:
                    terminate(process)
                    fail("Docker control output exceeded its bound")
        returncode = process.wait(timeout=max(1, deadline - time.monotonic()))
    except subprocess.TimeoutExpired:
        terminate(process)
        fail("Docker control command exceeded its timeout")
    finally:
        selector.close()
    if returncode != 0:
        fail("Docker control command failed")
    return bytes(output)


def json_value(raw: bytes, label: str) -> Any:
    try:
        return json.loads(raw)
    except (json.JSONDecodeError, UnicodeDecodeError) as exc:
        fail(f"Docker {label} output is not JSON: {exc}")


def reject_duplicate_object_keys(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    value: dict[str, Any] = {}
    for key, item in pairs:
        if key in value:
            fail(f"scorer attestation has duplicate key: {key}")
        value[key] = item
    return value


def regular_client_file(path: Path, *, maximum: int) -> os.stat_result:
    try:
        metadata = path.lstat()
    except OSError as exc:
        fail(f"scorer attestation is unavailable: {exc}")
    if not stat.S_ISREG(metadata.st_mode) or stat.S_ISLNK(metadata.st_mode):
        fail("scorer attestation is not a regular file")
    if (
        metadata.st_uid != 0
        or metadata.st_gid != client_group_id()
        or stat.S_IMODE(metadata.st_mode) != 0o640
    ):
        fail("scorer attestation ownership or mode is invalid")
    if metadata.st_size <= 0 or metadata.st_size > maximum:
        fail("scorer attestation size is outside its bound")
    return metadata


def read_scorer_attestation(path: Path) -> tuple[dict[str, str], str]:
    if path != EXPECTED_SCORER_ATTESTATION_PATH:
        fail("scorer-image attestation path is not fixed")
    metadata = regular_client_file(path, maximum=16 << 10)
    try:
        raw = path.read_bytes()
        current = path.lstat()
    except OSError as exc:
        fail(f"scorer attestation cannot be read: {exc}")
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
        fail("scorer attestation changed while being read")
    try:
        decoded = json.loads(raw, object_pairs_hook=reject_duplicate_object_keys)
    except (json.JSONDecodeError, UnicodeDecodeError) as exc:
        fail(f"scorer attestation is not strict JSON: {exc}")
    if not isinstance(decoded, dict) or set(decoded) != SCORER_ATTESTATION_FIELDS:
        fail("scorer attestation fields are invalid")
    value: dict[str, str] = {}
    for key, item in decoded.items():
        if not isinstance(item, str):
            fail(f"scorer attestation {key} is not a string")
        value[key] = item
    if value["schema"] != ATTESTATION_SCHEMA or value["platform"] != "linux/amd64":
        fail("scorer attestation schema or platform is invalid")
    if value["scorer_contract"] != "1":
        fail("scorer attestation contract is invalid")
    if not SOURCE_REVISION_RE.fullmatch(value["source_revision"]):
        fail("scorer attestation source revision is invalid")
    for field in (
        "archive_sha256",
        "bundle_manifest_sha256",
        "locked_policy_sha256",
        "release_manifest_sha256",
    ):
        if not SHA256_RE.fullmatch(value[field]):
            fail(f"scorer attestation {field} is invalid")
    if not OCI_DIGEST_RE.fullmatch(value["image_id"]):
        fail("scorer attestation image ID is invalid")
    repository, separator, digest = value["image_reference"].partition("@")
    if (
        repository != EXPECTED_IMAGE_REPOSITORY
        or separator != "@"
        or not OCI_DIGEST_RE.fullmatch(digest)
    ):
        fail("scorer attestation image reference is invalid")
    return value, hashlib.sha256(raw).hexdigest()


def daemon_has_label(value: Any, expected: str) -> bool:
    if isinstance(value, list):
        return expected in value
    if isinstance(value, dict):
        key, separator, expected_value = expected.partition("=")
        return separator == "=" and value.get(key) == expected_value
    return False


def inspect_daemon() -> None:
    security = json_value(
        docker_output(
            ["info", "--format", "{{json .SecurityOptions}}"],
            timeout=CONTROL_TIMEOUT_SECONDS,
        ),
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
        fail("coding Docker daemon is not rootless")
    labels = json_value(
        docker_output(
            ["info", "--format", "{{json .Labels}}"],
            timeout=CONTROL_TIMEOUT_SECONDS,
        ),
        "labels",
    )
    if not daemon_has_label(labels, ISOLATED_DAEMON_LABEL):
        fail("coding Docker daemon lacks the isolated ownership label")


def valid_image_id(value: Any) -> bool:
    return isinstance(value, str) and OCI_DIGEST_RE.fullmatch(value) is not None


def credential_image_environment(value: str) -> bool:
    name = value.partition("=")[0].upper()
    if name in {"HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY"}:
        return True
    return any(
        marker in name
        for marker in ("KEY", "TOKEN", "SECRET", "PASSWORD", "CREDENTIAL")
    )


def inspect_scorer_image(attestation: dict[str, str]) -> None:
    raw = docker_output(
        ["image", "inspect", "--format", "{{json .}}", attestation["image_id"]],
        timeout=CONTROL_TIMEOUT_SECONDS,
    )
    image = json_value(raw, "scorer-image inspection")
    if not isinstance(image, dict) or image.get("Id") != attestation["image_id"]:
        fail("scorer image ID does not match the attestation")
    if not valid_image_id(image.get("Id")):
        fail("scorer image ID is invalid")
    if image.get("Os") != "linux" or image.get("Architecture") != "amd64":
        fail("scorer image platform is invalid")
    config = image.get("Config")
    if not isinstance(config, dict) or config.get("User") != SCORER_USER:
        fail("scorer image user is invalid")
    if config.get("Entrypoint") != [SCORER_ENTRYPOINT]:
        fail("scorer image entrypoint is invalid")
    if config.get("Volumes") not in (None, {}) or config.get("ExposedPorts") not in (
        None,
        {},
    ):
        fail("scorer image declares a volume or exposed port")
    if config.get("Healthcheck") not in (None, {}) or config.get("Cmd") not in (
        None,
        [],
    ):
        fail("scorer image declares an unexpected healthcheck or command")
    labels = config.get("Labels")
    if not isinstance(labels, dict) or not all(
        isinstance(key, str) and isinstance(value, str) for key, value in labels.items()
    ):
        fail("scorer image labels are invalid")
    if labels.get(SCORER_CONTRACT_LABEL) != attestation["scorer_contract"]:
        fail("scorer image contract drifted")
    if labels.get(SCORER_POLICY_LABEL) != attestation["locked_policy_sha256"]:
        fail("scorer image locked policy drifted")
    if labels.get(SOURCE_REVISION_LABEL) != attestation["source_revision"]:
        fail("scorer image source revision drifted")
    environment = config.get("Env")
    if not isinstance(environment, list) or not all(
        isinstance(value, str) for value in environment
    ):
        fail("scorer image environment is invalid")
    if any(credential_image_environment(value) for value in environment):
        fail("scorer image has credential-shaped environment")


def secure_root_directory(path: Path) -> None:
    try:
        metadata = path.lstat()
    except OSError as exc:
        fail(f"cannot inspect scorer runtime directory: {exc}")
    if not stat.S_ISDIR(metadata.st_mode) or stat.S_ISLNK(metadata.st_mode):
        fail("scorer runtime directory is not a directory")
    if metadata.st_uid != 0 or metadata.st_gid != 0 or metadata.st_mode & 0o022:
        fail("scorer runtime directory ownership or mode is invalid")


def sha256_file(path: Path, *, maximum: int) -> str:
    try:
        metadata = path.lstat()
    except OSError as exc:
        fail(f"cannot inspect extracted scorer runtime file: {exc}")
    if not stat.S_ISREG(metadata.st_mode) or stat.S_ISLNK(metadata.st_mode):
        fail("extracted scorer runtime path is not a regular file")
    if metadata.st_size <= 0 or metadata.st_size > maximum:
        fail("extracted scorer runtime file size is outside its bound")
    digest = hashlib.sha256()
    remaining = metadata.st_size
    try:
        with path.open("rb", buffering=0) as source:
            while remaining:
                block = source.read(min(1 << 20, remaining))
                if not block:
                    fail("extracted scorer runtime file changed while being hashed")
                digest.update(block)
                remaining -= len(block)
        current = path.lstat()
    except OSError as exc:
        fail(f"cannot hash extracted scorer runtime file: {exc}")
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
        fail("extracted scorer runtime file changed while being verified")
    return digest.hexdigest()


def validate_binary(path: Path) -> str:
    digest = sha256_file(path, maximum=256 << 20)
    try:
        header = path.read_bytes()[:20]
    except OSError as exc:
        fail(f"cannot read scorer binary header: {exc}")
    if (
        len(header) != 20
        or header[:4] != b"\x7fELF"
        or header[4] != 2
        or header[5] != 1
        or int.from_bytes(header[18:20], "little") != 62
    ):
        fail("extracted scorer binary is not a linux/amd64 ELF executable")
    return digest


def validate_policy(path: Path) -> str:
    digest = sha256_file(path, maximum=64 << 10)
    if digest != LOCKED_POLICY_FILE_SHA256:
        fail("extracted scorer policy does not match the locked policy file")
    return digest


def install_file(source: Path, destination: Path, *, mode: int, maximum: int) -> bool:
    secure_root_directory(destination.parent)
    source_digest = sha256_file(source, maximum=maximum)
    if destination.is_symlink():
        fail("installed scorer runtime path is a symlink")
    if destination.exists():
        try:
            metadata = destination.lstat()
        except OSError as exc:
            fail(f"cannot inspect installed scorer runtime file: {exc}")
        if (
            not stat.S_ISREG(metadata.st_mode)
            or stat.S_ISLNK(metadata.st_mode)
            or metadata.st_uid != 0
            or metadata.st_gid != 0
            or stat.S_IMODE(metadata.st_mode) != mode
        ):
            fail("installed scorer runtime file ownership or mode is invalid")
        if sha256_file(destination, maximum=maximum) == source_digest:
            return False
    temporary_path: Path | None = None
    try:
        with (
            source.open("rb", buffering=0) as input_file,
            tempfile.NamedTemporaryFile(dir=destination.parent, delete=False) as output,
        ):
            temporary_path = Path(output.name)
            shutil.copyfileobj(input_file, output, length=1 << 20)
            output.flush()
            os.fsync(output.fileno())
        os.chown(temporary_path, 0, 0)
        os.chmod(temporary_path, mode)
        os.replace(temporary_path, destination)
        directory_fd = os.open(destination.parent, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)
    except OSError as exc:
        if temporary_path is not None:
            temporary_path.unlink(missing_ok=True)
        fail(f"cannot install scorer runtime file: {exc}")
    return True


def write_runtime_attestation(value: dict[str, str]) -> bool:
    path = EXPECTED_RUNTIME_ATTESTATION_PATH
    parent = path.parent
    try:
        metadata = parent.lstat()
    except OSError as exc:
        fail(f"cannot inspect scorer runtime attestation directory: {exc}")
    if (
        not stat.S_ISDIR(metadata.st_mode)
        or stat.S_ISLNK(metadata.st_mode)
        or metadata.st_uid != 0
        or metadata.st_gid != client_group_id()
        or stat.S_IMODE(metadata.st_mode) != 0o750
    ):
        fail("scorer runtime attestation directory ownership or mode is invalid")
    encoded = (json.dumps(value, sort_keys=True, separators=(",", ":")) + "\n").encode()
    if path.is_symlink():
        fail("scorer runtime attestation path is a symlink")
    if path.exists():
        metadata = regular_client_file(path, maximum=16 << 10)
        try:
            if path.read_bytes() == encoded:
                return False
        except OSError as exc:
            fail(f"cannot read scorer runtime attestation: {exc}")
        if metadata.st_size > 16 << 10:
            fail("scorer runtime attestation size is outside its bound")
    temporary_path: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(dir=parent, delete=False) as output:
            temporary_path = Path(output.name)
            output.write(encoded)
            output.flush()
            os.fsync(output.fileno())
        os.chown(temporary_path, 0, client_group_id())
        os.chmod(temporary_path, 0o640)
        os.replace(temporary_path, path)
        directory_fd = os.open(parent, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)
    except OSError as exc:
        if temporary_path is not None:
            temporary_path.unlink(missing_ok=True)
        fail(f"cannot write scorer runtime attestation: {exc}")
    return True


def copy_attested_runtime(attestation: dict[str, str]) -> tuple[bool, bool, str, str]:
    created = (
        docker_output(
            ["create", "--network", "none", attestation["image_id"]],
            timeout=CONTROL_TIMEOUT_SECONDS,
        )
        .decode(errors="replace")
        .strip()
    )
    if not CONTAINER_ID_RE.fullmatch(created):
        fail("temporary scorer container ID is invalid")
    cleanup_error: MaterializationError | None = None
    try:
        with tempfile.TemporaryDirectory(
            dir=EXPECTED_RUNTIME_ATTESTATION_PATH.parent,
            prefix="scorer-runtime-",
        ) as temporary:
            destination = Path(temporary)
            docker_output(
                ["cp", created + ":" + SCORER_IMAGE_BINARY_PATH, str(destination)],
                timeout=CONTROL_TIMEOUT_SECONDS,
            )
            docker_output(
                ["cp", created + ":" + SCORER_IMAGE_POLICY_PATH, str(destination)],
                timeout=CONTROL_TIMEOUT_SECONDS,
            )
            binary = destination / SCORER_BINARY_NAME
            policy = destination / EXPECTED_RUNTIME_POLICY_PATH.name
            binary_digest = validate_binary(binary)
            policy_digest = validate_policy(policy)
            binary_changed = install_file(
                binary,
                EXPECTED_RUNTIME_BINARY_PATH,
                mode=0o755,
                maximum=256 << 20,
            )
            policy_changed = install_file(
                policy,
                EXPECTED_RUNTIME_POLICY_PATH,
                mode=0o444,
                maximum=64 << 10,
            )
            return binary_changed, policy_changed, binary_digest, policy_digest
    finally:
        try:
            docker_output(["rm", "-f", created], timeout=CONTROL_TIMEOUT_SECONDS)
        except MaterializationError as exc:
            cleanup_error = exc
        if cleanup_error is not None:
            raise cleanup_error


def materialize_scorer_runtime() -> bool:
    if os.geteuid() != 0:
        fail("scorer runtime materializer must run as root")
    attestation, attestation_sha256 = read_scorer_attestation(
        EXPECTED_SCORER_ATTESTATION_PATH
    )
    inspect_daemon()
    inspect_scorer_image(attestation)
    binary_changed, policy_changed, binary_sha256, policy_file_sha256 = (
        copy_attested_runtime(attestation)
    )
    runtime_attestation = {
        "binary_sha256": binary_sha256,
        "locked_policy_sha256": attestation["locked_policy_sha256"],
        "policy_file_sha256": policy_file_sha256,
        "schema": RUNTIME_SCHEMA,
        "scorer_attestation_sha256": attestation_sha256,
        "scorer_contract": attestation["scorer_contract"],
        "scorer_image_id": attestation["image_id"],
        "scorer_image_reference": attestation["image_reference"],
        "source_revision": attestation["source_revision"],
    }
    attestation_changed = write_runtime_attestation(runtime_attestation)
    return binary_changed or policy_changed or attestation_changed


def main() -> int:
    changed = materialize_scorer_runtime()
    print(f"changed={'true' if changed else 'false'}")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except MaterializationError as exc:
        print(f"scorer runtime materialization failed: {exc}", file=sys.stderr)
        raise SystemExit(1) from None
