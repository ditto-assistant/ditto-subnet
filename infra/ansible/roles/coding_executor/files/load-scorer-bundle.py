#!/usr/bin/env python3
"""Load and attest one already-verified coding executor scorer image.

This root-only transition contacts only the dedicated rootless Docker socket.
It does not run a container, start the scorer, add a socket client, or acquire
any registry, provider, Platform, validator, wallet, or ticket authority.
"""

from __future__ import annotations

import argparse
import grp
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
ATTESTATION_SCHEMA = "dittobench-coding-executor-scorer-image-attestation-v1"
EXPECTED_DOCKER_HOST = "unix:///run/ditto-coding-executor/docker.sock"
EXPECTED_RELEASE_MANIFEST_PATH = Path(
    "/var/lib/ditto-coding-executor/scorer-staged/scorer.release.json"
)
EXPECTED_BUNDLE_MANIFEST_PATH = Path(
    "/var/lib/ditto-coding-executor/scorer-staged/scorer.bundle.json"
)
EXPECTED_ARCHIVE_PATH = Path(
    "/var/lib/ditto-coding-executor/scorer-staged/scorer.oci.tar"
)
EXPECTED_ATTESTATION_PATH = Path(
    "/var/lib/ditto-coding-executor/attestations/scorer-image-attestation.json"
)
EXPECTED_CLIENT_GROUP = "ditto-coding-client"
ISOLATED_DAEMON_LABEL = "io.heyditto.dittobench.isolated=true"
SCORER_CONTRACT_LABEL = "io.heyditto.dittobench.coding-executor-scorer-contract"
SCORER_POLICY_LABEL = "io.heyditto.dittobench.coding-executor-locked-policy-sha256"
SOURCE_REVISION_LABEL = "org.opencontainers.image.revision"
SCORER_ENTRYPOINT = "/dittobench-coding-executor-scorer"
SCORER_USER = "65532:65532"


class LoaderError(ValueError):
    """Raised when the rootless daemon cannot prove the exact scorer image."""


def load_bundle_verifier() -> Any:
    path = Path(__file__).with_name("verify-scorer-bundle.py")
    specification = importlib.util.spec_from_file_location(
        "scorer_bundle_verifier", path
    )
    if specification is None or specification.loader is None:
        raise LoaderError("scorer-bundle verifier cannot be loaded")
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
    parser.add_argument("--release-manifest", required=True, type=Path)
    parser.add_argument("--bundle-manifest", required=True, type=Path)
    parser.add_argument("--archive", required=True, type=Path)
    parser.add_argument("--expected-bundle-sha256", required=True)
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


def client_group_id() -> int:
    try:
        return grp.getgrnam(EXPECTED_CLIENT_GROUP).gr_gid
    except KeyError:
        fail("dedicated Docker client group is unavailable")


def secure_attestation_directory(path: Path) -> None:
    try:
        metadata = path.lstat()
    except OSError as exc:
        fail(f"cannot inspect scorer-image attestation directory: {exc}")
    if not stat.S_ISDIR(metadata.st_mode) or stat.S_ISLNK(metadata.st_mode):
        fail("scorer-image attestation directory is not a directory")
    if (
        metadata.st_uid != 0
        or metadata.st_gid != client_group_id()
        or stat.S_IMODE(metadata.st_mode) != 0o750
    ):
        fail("scorer-image attestation directory ownership or mode is invalid")


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
                    fail("scorer-image archive has an unsafe member type")
                total_size += member.size
                if total_size > MAX_UNPACKED_ARCHIVE_BYTES:
                    fail("scorer-image archive exceeds its unpacked-size bound")
    except (OSError, tarfile.TarError) as exc:
        fail(f"scorer-image archive is not a readable tar archive: {exc}")


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


def validate_loaded_image(
    image: Any,
    manifest: dict[str, Any],
    expected_image_id: str,
) -> dict[str, str]:
    if not isinstance(image, dict):
        fail("Docker scorer-image inspection is not an object")
    expected_reference = manifest["image_reference"]
    if not valid_image_id(image.get("Id")) or image["Id"] != expected_image_id:
        fail("loaded scorer image ID does not match the verified export bundle")
    if image.get("Os") != "linux" or image.get("Architecture") != "amd64":
        fail("loaded scorer image platform is not linux/amd64")
    config = image.get("Config")
    if not isinstance(config, dict):
        fail("loaded scorer image config is invalid")
    if config.get("User") != SCORER_USER:
        fail("loaded scorer image user is not the fixed non-root identity")
    labels = config.get("Labels")
    if not isinstance(labels, dict) or not all(
        isinstance(key, str) and isinstance(value, str) for key, value in labels.items()
    ):
        fail("loaded scorer image labels are invalid")
    if labels.get(SCORER_CONTRACT_LABEL) != manifest["scorer_contract"]:
        fail("loaded scorer image contract does not match the release manifest")
    if labels.get(SCORER_POLICY_LABEL) != manifest["locked_policy_sha256"]:
        fail("loaded scorer image locked policy does not match the release manifest")
    if labels.get(SOURCE_REVISION_LABEL) != manifest["source_revision"]:
        fail("loaded scorer image source revision does not match the release manifest")
    if config.get("Volumes") not in (None, {}):
        fail("loaded scorer image declares a volume")
    if config.get("ExposedPorts") not in (None, {}):
        fail("loaded scorer image declares an exposed port")
    if config.get("Healthcheck") not in (None, {}):
        fail("loaded scorer image declares an embedded healthcheck")
    if config.get("Cmd") not in (None, []):
        fail("loaded scorer image declares an unexpected command")
    environment = config.get("Env")
    if not isinstance(environment, list) or not all(
        isinstance(value, str) for value in environment
    ):
        fail("loaded scorer image environment is invalid")
    if any(credential_image_environment(value) for value in environment):
        fail("loaded scorer image has credential-shaped environment")
    if config.get("Entrypoint") != [SCORER_ENTRYPOINT]:
        fail("loaded scorer image entrypoint is invalid")
    return {
        "image_id": image["Id"],
        "image_reference": expected_reference,
        "locked_policy_sha256": manifest["locked_policy_sha256"],
        "platform": "linux/amd64",
        "scorer_contract": manifest["scorer_contract"],
        "source_revision": manifest["source_revision"],
    }


def regular_existing_attestation(path: Path) -> os.stat_result:
    metadata = path.lstat()
    if not stat.S_ISREG(metadata.st_mode) or stat.S_ISLNK(metadata.st_mode):
        fail("scorer-image attestation path is not a regular file")
    if (
        metadata.st_uid != 0
        or metadata.st_gid != client_group_id()
        or stat.S_IMODE(metadata.st_mode) != 0o640
    ):
        fail("existing scorer-image attestation ownership or mode is invalid")
    return metadata


def write_attestation(path: Path, value: dict[str, str]) -> bool:
    encoded = (json.dumps(value, sort_keys=True, separators=(",", ":")) + "\n").encode()
    if path.is_symlink() or (path.exists() and not stat.S_ISREG(path.lstat().st_mode)):
        fail("scorer-image attestation path is not a regular file")
    if path.exists():
        regular_existing_attestation(path)
        try:
            if path.read_bytes() == encoded:
                return False
        except OSError as exc:
            fail(f"cannot read existing scorer-image attestation: {exc}")
    temporary_path: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(dir=path.parent, delete=False) as output:
            temporary_path = Path(output.name)
            output.write(encoded)
            output.flush()
            os.fsync(output.fileno())
        os.chown(temporary_path, 0, client_group_id())
        os.chmod(temporary_path, 0o640)
        os.replace(temporary_path, path)
        directory_fd = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)
    except OSError as exc:
        if temporary_path is not None:
            temporary_path.unlink(missing_ok=True)
        fail(f"cannot write scorer-image attestation: {exc}")
    return True


def load_scorer_bundle(
    release_manifest_path: Path,
    bundle_manifest_path: Path,
    archive_path: Path,
    expected_bundle_sha256: str,
    docker_host: str,
    attestation_path: Path,
) -> bool:
    if os.geteuid() != 0:
        fail("scorer-bundle loader must run as root")
    if docker_host != EXPECTED_DOCKER_HOST:
        fail("coding Docker host is not the fixed dedicated Unix socket")
    if (
        release_manifest_path != EXPECTED_RELEASE_MANIFEST_PATH
        or bundle_manifest_path != EXPECTED_BUNDLE_MANIFEST_PATH
        or archive_path != EXPECTED_ARCHIVE_PATH
        or attestation_path != EXPECTED_ATTESTATION_PATH
    ):
        fail("scorer-image paths are not the fixed protected paths")
    secure_attestation_directory(attestation_path.parent)
    bundle = BUNDLE_VERIFIER.verify_scorer_bundle(
        release_manifest_path,
        bundle_manifest_path,
        archive_path,
        expected_bundle_sha256,
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
        ["image", "inspect", bundle.bundle_manifest["image_id"]],
        timeout=CONTROL_TIMEOUT_SECONDS,
    )
    image = json_value(image_raw, "scorer-image inspection")
    if not isinstance(image, list) or len(image) != 1:
        fail("Docker scorer-image inspection did not return exactly one image")
    attestation = validate_loaded_image(
        image[0],
        bundle.release_manifest,
        bundle.bundle_manifest["image_id"],
    )
    attestation.update(
        {
            "archive_sha256": bundle.archive_sha256,
            "bundle_manifest_sha256": bundle.bundle_manifest_sha256,
            "release_manifest_sha256": bundle.release_manifest_sha256,
            "schema": ATTESTATION_SCHEMA,
        }
    )
    return write_attestation(attestation_path, attestation)


def main() -> int:
    args = parse_args()
    changed = load_scorer_bundle(
        args.release_manifest,
        args.bundle_manifest,
        args.archive,
        args.expected_bundle_sha256,
        args.docker_host,
        args.attestation,
    )
    print(f"changed={'true' if changed else 'false'}")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (BUNDLE_VERIFIER.VerificationError, LoaderError) as exc:
        print(f"scorer-image load failed: {exc}", file=sys.stderr)
        raise SystemExit(1) from None
