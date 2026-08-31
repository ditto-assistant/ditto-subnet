#!/usr/bin/env python3
"""Load and attest one already-verified production coding runtime image.

This runs only under the dedicated rootless Docker daemon. It never runs a
container, starts a scorer, or changes the daemon-client group. A root-owned
attestation is written only after the loaded image has the exact registry
digest and safety properties the later Go executor will require.
"""

from __future__ import annotations

import argparse
import importlib.util
import json
import os
import selectors
import stat
import subprocess
import sys
import tarfile
import tempfile
import time
from pathlib import Path
from typing import Any

MAX_DOCKER_OUTPUT = 1 << 20
MAX_UNPACKED_ARCHIVE_BYTES = 16 << 30
LOAD_TIMEOUT_SECONDS = 15 * 60
CONTROL_TIMEOUT_SECONDS = 60
ATTESTATION_SCHEMA = "dittobench-coding-runtime-image-attestation-v1"
EXPECTED_DOCKER_HOST = "unix:///run/ditto-coding-executor/docker.sock"
EXPECTED_MANIFEST_PATH = Path(
    "/var/lib/ditto-coding-executor/staged/runtime-manifest.json"
)
EXPECTED_ARCHIVE_PATH = Path("/var/lib/ditto-coding-executor/staged/supervisor.oci.tar")
EXPECTED_ATTESTATION_PATH = Path(
    "/var/lib/ditto-coding-executor/staged/runtime-image-attestation.json"
)
SUPERVISOR_ENTRYPOINT = "/usr/local/bin/dittobench-coding-supervisor"
TRUSTED_DRIVER_NAME = "dittobench-test-driver"
ISOLATED_DAEMON_LABEL = "io.heyditto.dittobench.isolated=true"
SUPERVISOR_CONTRACT_LABEL = "io.heyditto.dittobench.coding-supervisor-contract"
FIXTURE_LABEL = "io.heyditto.dittobench.coding-supervisor-fixture"
TRUSTED_DRIVER_DIGEST_LABEL = "io.heyditto.dittobench.trusted-test-driver-sha256"
TRUSTED_DRIVER_NAME_LABEL = "io.heyditto.dittobench.trusted-test-driver-name"
SOURCE_REVISION_LABEL = "org.opencontainers.image.revision"


class LoaderError(ValueError):
    """Raised when the rootless daemon cannot prove an exact safe image."""


def load_bundle_verifier() -> Any:
    path = Path(__file__).with_name("verify-runtime-bundle.py")
    specification = importlib.util.spec_from_file_location(
        "runtime_bundle_verifier", path
    )
    if specification is None or specification.loader is None:
        raise LoaderError("runtime-bundle verifier cannot be loaded")
    module = importlib.util.module_from_spec(specification)
    sys.modules[specification.name] = module
    try:
        specification.loader.exec_module(module)
    except Exception:
        sys.modules.pop(specification.name, None)
        raise
    return module


BUNDLE_VERIFIER = load_bundle_verifier()


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--manifest", required=True, type=Path)
    parser.add_argument("--archive", required=True, type=Path)
    parser.add_argument("--expected-manifest-sha256", required=True)
    parser.add_argument("--docker-host", required=True)
    parser.add_argument("--attestation", required=True, type=Path)
    return parser.parse_args()


def fail(message: str) -> None:
    raise LoaderError(message)


def terminate(process: subprocess.Popen[bytes]) -> None:
    process.terminate()
    try:
        process.wait(timeout=5)
    except subprocess.TimeoutExpired:
        process.kill()
        process.wait()


def docker_output(docker_host: str, arguments: list[str], *, timeout: int) -> bytes:
    command = ["/usr/bin/docker", "--host", docker_host, *arguments]
    try:
        process = subprocess.Popen(
            command,
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


def valid_image_id(value: Any) -> bool:
    if not isinstance(value, str) or not value.startswith("sha256:"):
        return False
    digest = value.removeprefix("sha256:")
    if len(digest) != 64 or digest != digest.lower():
        return False
    try:
        int(digest, 16)
    except ValueError:
        return False
    return True


def require_string(value: Any, label: str) -> str:
    if not isinstance(value, str):
        fail(f"Docker image {label} is not a string")
    return value


def secure_root_owned_directory(path: Path) -> None:
    try:
        metadata = path.lstat()
    except OSError as exc:
        fail(f"cannot inspect runtime-image staging directory: {exc}")
    if not stat.S_ISDIR(metadata.st_mode) or stat.S_ISLNK(metadata.st_mode):
        fail("runtime-image staging directory is not a directory")
    if metadata.st_uid != 0 or metadata.st_gid != 0 or metadata.st_mode & 0o077:
        fail("runtime-image staging directory is not root-owned mode 0700")


def validate_archive_layout(path: Path) -> None:
    total_size = 0
    try:
        with tarfile.open(path, mode="r:*") as archive:
            for member in archive:
                if member.isdir():
                    continue
                if (
                    not member.isfile()
                    or member.issym()
                    or member.islnk()
                    or member.isdev()
                ):
                    fail("runtime-image archive has an unsafe member type")
                total_size += member.size
                if total_size > MAX_UNPACKED_ARCHIVE_BYTES:
                    fail("runtime-image archive exceeds its unpacked-size bound")
    except (OSError, tarfile.TarError) as exc:
        fail(f"runtime-image archive is not a readable tar archive: {exc}")


def inspect_daemon(docker_host: str) -> None:
    security = json_value(
        docker_output(
            docker_host,
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
            docker_host,
            ["info", "--format", "{{json .Labels}}"],
            timeout=CONTROL_TIMEOUT_SECONDS,
        ),
        "labels",
    )
    if not daemon_has_label(labels, ISOLATED_DAEMON_LABEL):
        fail("coding Docker daemon lacks the isolated ownership label")


def validate_loaded_image(image: Any, manifest: dict[str, Any]) -> dict[str, str]:
    if not isinstance(image, dict):
        fail("Docker image inspection is not an object")
    expected_reference = manifest["image_repository"] + "@" + manifest["image_digest"]
    repo_digests = image.get("RepoDigests")
    if not isinstance(repo_digests, list) or expected_reference not in repo_digests:
        fail("loaded image does not retain the exact manifest repository digest")
    if not valid_image_id(image.get("Id")):
        fail("loaded image ID is invalid")
    if image.get("Os") != "linux" or image.get("Architecture") != "amd64":
        fail("loaded image platform is not linux/amd64")
    config = image.get("Config")
    if not isinstance(config, dict):
        fail("loaded image config is invalid")
    labels = config.get("Labels")
    if not isinstance(labels, dict) or not all(
        isinstance(key, str) and isinstance(value, str) for key, value in labels.items()
    ):
        fail("loaded image labels are invalid")
    if labels.get(SUPERVISOR_CONTRACT_LABEL) != manifest["supervisor_contract"]:
        fail("loaded image supervisor contract does not match the manifest")
    if labels.get(FIXTURE_LABEL) == "true":
        fail("loaded image is the public certification fixture")
    if (
        labels.get(TRUSTED_DRIVER_DIGEST_LABEL)
        != manifest["trusted_test_driver_digest"]
    ):
        fail("loaded image trusted-driver digest does not match the manifest")
    if labels.get(TRUSTED_DRIVER_NAME_LABEL) != TRUSTED_DRIVER_NAME:
        fail("loaded image trusted-driver name is invalid")
    if labels.get(SOURCE_REVISION_LABEL) != manifest["source_revision"]:
        fail("loaded image source revision does not match the manifest")
    if config.get("Volumes") not in (None, {}):
        fail("loaded image declares a volume")
    environment = config.get("Env")
    if not isinstance(environment, list) or not all(
        isinstance(value, str) for value in environment
    ):
        fail("loaded image environment is invalid")
    if any(credential_image_environment(value) for value in environment):
        fail("loaded image has credential-shaped environment")
    if config.get("Entrypoint") != [SUPERVISOR_ENTRYPOINT]:
        fail("loaded image supervisor entrypoint is invalid")
    return {
        "image_id": image["Id"],
        "image_reference": expected_reference,
        "platform": "linux/amd64",
        "source_revision": manifest["source_revision"],
        "supervisor_contract": manifest["supervisor_contract"],
        "trusted_test_driver_digest": manifest["trusted_test_driver_digest"],
    }


def write_attestation(path: Path, value: dict[str, str]) -> None:
    if path.is_symlink() or (path.exists() and not stat.S_ISREG(path.lstat().st_mode)):
        fail("runtime-image attestation path is not a regular file")
    try:
        encoded = (
            json.dumps(value, sort_keys=True, separators=(",", ":")) + "\n"
        ).encode()
        with tempfile.NamedTemporaryFile(dir=path.parent, delete=False) as output:
            temporary_path = Path(output.name)
            output.write(encoded)
            output.flush()
            os.fsync(output.fileno())
        os.chown(temporary_path, 0, 0)
        os.chmod(temporary_path, 0o600)
        os.replace(temporary_path, path)
    except OSError as exc:
        fail(f"cannot write runtime-image attestation: {exc}")


def load_runtime_bundle(
    manifest_path: Path,
    archive_path: Path,
    expected_manifest_sha256: str,
    docker_host: str,
    attestation_path: Path,
) -> None:
    if os.geteuid() != 0:
        fail("runtime-bundle loader must run as root")
    if docker_host != EXPECTED_DOCKER_HOST:
        fail("coding Docker host is not the fixed dedicated Unix socket")
    if (
        manifest_path != EXPECTED_MANIFEST_PATH
        or archive_path != EXPECTED_ARCHIVE_PATH
        or attestation_path != EXPECTED_ATTESTATION_PATH
    ):
        fail("runtime-image paths are not the fixed protected staging paths")
    secure_root_owned_directory(attestation_path.parent)
    bundle = BUNDLE_VERIFIER.verify_runtime_bundle(
        manifest_path,
        archive_path,
        expected_manifest_sha256,
    )
    validate_archive_layout(archive_path)
    inspect_daemon(docker_host)
    docker_output(
        docker_host,
        ["image", "load", "--input", str(archive_path)],
        timeout=LOAD_TIMEOUT_SECONDS,
    )
    image_raw = docker_output(
        docker_host,
        [
            "image",
            "inspect",
            bundle.manifest["image_repository"] + "@" + bundle.manifest["image_digest"],
        ],
        timeout=CONTROL_TIMEOUT_SECONDS,
    )
    image = json_value(image_raw, "image inspection")
    if not isinstance(image, list) or len(image) != 1:
        fail("Docker image inspection did not return exactly one image")
    attestation = validate_loaded_image(image[0], bundle.manifest)
    attestation.update(
        {
            "archive_sha256": bundle.archive_sha256,
            "manifest_sha256": bundle.manifest_sha256,
            "schema": ATTESTATION_SCHEMA,
        }
    )
    write_attestation(attestation_path, attestation)


def main() -> int:
    args = parse_args()
    load_runtime_bundle(
        args.manifest,
        args.archive,
        args.expected_manifest_sha256,
        args.docker_host,
        args.attestation,
    )
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (BUNDLE_VERIFIER.VerificationError, LoaderError) as exc:
        print(f"runtime-image load failed: {exc}", file=sys.stderr)
        raise SystemExit(1) from None
