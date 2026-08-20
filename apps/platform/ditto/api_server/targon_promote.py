"""Promote a Platform-verified miner archive into the candidate registry.

Runs on the Platform VM. The Targon API key never enters this path; only a
short-lived impersonated Artifact Registry token is used as pull/push auth.
"""

from __future__ import annotations

import asyncio
import base64
import gzip
import json
import os
import re
import shutil
import subprocess
import tarfile
import tempfile
from pathlib import Path

from ditto.api_server.storage.client import S3StorageClient
from ditto.api_server.storage.errors import ObjectDownloadFailedError

_SKOPEO_TIMEOUT_SECONDS = 600
_INSPECT_TIMEOUT_SECONDS = 60
_MINT_TIMEOUT_SECONDS = 30


class TargonPromoteError(RuntimeError):
    """Registry promotion or token mint failed without leaking credentials."""


def mint_access_token(service_account: str) -> str:
    """Mint a 30-minute access token by impersonating ``service_account``."""
    try:
        result = subprocess.run(
            [
                "gcloud",
                "auth",
                "print-access-token",
                f"--impersonate-service-account={service_account}",
                "--lifetime=1800",
                "--quiet",
            ],
            check=True,
            capture_output=True,
            text=True,
            timeout=_MINT_TIMEOUT_SECONDS,
        )
    except (OSError, subprocess.SubprocessError) as error:
        raise TargonPromoteError("registry token mint failed") from error
    token = result.stdout.strip()
    if len(token) < 100:
        raise TargonPromoteError("registry token mint returned invalid data")
    return token


def inspect_registry_config_digest(image_reference: str, access_token: str) -> str:
    """Return the image-config digest from a registry manifest.

    Validators still fetch the docker-save tar via a presigned URL. Platform
    only needs the config digest so DittoBench can match ``{configDigest}.json``.
    This is a small ``skopeo inspect --raw``, not an archive download.
    """
    if "/" not in image_reference or "@" not in image_reference:
        raise TargonPromoteError("runtime image reference is invalid")
    registry_host = image_reference.split("/", 1)[0]
    result = _run_skopeo(
        ["skopeo", "inspect", "--raw", f"docker://{image_reference}"],
        registry_host=registry_host,
        access_token=access_token,
        timeout=_INSPECT_TIMEOUT_SECONDS,
    )
    if result.returncode != 0:
        inspect_error = subprocess.CalledProcessError(
            result.returncode, result.args, result.stdout, result.stderr
        )
        raise TargonPromoteError(
            f"runtime image inspect failed: {_skopeo_detail(inspect_error)}"
        ) from inspect_error
    try:
        payload = json.loads(result.stdout)
    except (TypeError, ValueError) as error:
        raise TargonPromoteError(
            "runtime image inspect returned invalid JSON"
        ) from error
    if not isinstance(payload, dict):
        raise TargonPromoteError("runtime image inspect returned invalid JSON")
    media_type = payload.get("mediaType")
    if media_type in (
        "application/vnd.oci.image.index.v1+json",
        "application/vnd.docker.distribution.manifest.list.v2+json",
    ):
        raise TargonPromoteError("runtime image inspect returned an index")
    config = payload.get("config")
    if not isinstance(config, dict):
        raise TargonPromoteError("runtime image manifest has no config")
    digest = config.get("digest")
    if not isinstance(digest, str) or not re.fullmatch(r"sha256:[0-9a-f]{64}", digest):
        raise TargonPromoteError("runtime image config digest is invalid")
    return digest


async def promote_runtime_archive(
    *,
    storage: S3StorageClient,
    source_key: str,
    destination: str,
    access_token: str,
) -> str:
    """Download ``source_key`` and copy it to ``destination``; return digest ref."""
    descriptor, raw_path = tempfile.mkstemp(prefix="ditto-runtime-", suffix=".tar")
    os.close(descriptor)
    archive = Path(raw_path)
    try:
        os.chmod(archive, 0o600)
        await storage.download_object_to_path(key=source_key, dest=archive)
        return await asyncio.to_thread(
            _promote_runtime_archive,
            archive=archive,
            destination=destination,
            access_token=access_token,
        )
    except ObjectDownloadFailedError as error:
        raise TargonPromoteError("runtime artifact download failed") from error
    finally:
        archive.unlink(missing_ok=True)


def _promote_runtime_archive(
    *, archive: Path, destination: str, access_token: str
) -> str:
    unpacked: Path | None = None
    try:
        unpacked = _materialize_image_archive(archive)
        last_error: BaseException | None = None
        copied = False
        registry_host = destination.split("/", 1)[0]
        for source in _image_archive_sources(unpacked):
            result = _run_skopeo(
                ["skopeo", "copy", source, f"docker://{destination}"],
                registry_host=registry_host,
                access_token=access_token,
                timeout=_SKOPEO_TIMEOUT_SECONDS,
            )
            if result.returncode == 0:
                copied = True
                break
            last_error = subprocess.CalledProcessError(
                result.returncode, result.args, result.stdout, result.stderr
            )
        if not copied:
            assert last_error is not None
            raise TargonPromoteError(
                f"runtime image promotion failed: {_skopeo_detail(last_error)}"
            ) from last_error
        inspect = _run_skopeo(
            [
                "skopeo",
                "inspect",
                "--format",
                "{{.Digest}}",
                f"docker://{destination}",
            ],
            registry_host=registry_host,
            access_token=access_token,
            timeout=_INSPECT_TIMEOUT_SECONDS,
        )
        if inspect.returncode != 0:
            inspect_error = subprocess.CalledProcessError(
                inspect.returncode, inspect.args, inspect.stdout, inspect.stderr
            )
            raise TargonPromoteError(
                f"runtime image promotion failed: {_skopeo_detail(inspect_error)}"
            ) from inspect_error
    except TargonPromoteError:
        raise
    except (OSError, subprocess.SubprocessError, ObjectDownloadFailedError) as error:
        raise TargonPromoteError(
            f"runtime image promotion failed: {_skopeo_detail(error)}"
        ) from error
    finally:
        if unpacked is not None and unpacked != archive:
            unpacked.unlink(missing_ok=True)
    digest = inspect.stdout.strip()
    if not re.fullmatch(r"sha256:[0-9a-f]{64}", digest):
        raise TargonPromoteError("runtime image promotion returned invalid digest")
    return f"{destination.rsplit(':', 1)[0]}@{digest}"


def _materialize_image_archive(archive: Path) -> Path:
    with archive.open("rb") as handle:
        magic = handle.read(2)
    if magic != b"\x1f\x8b":
        return archive
    unpacked = archive.with_name(f"{archive.name}.plain")
    with gzip.open(archive, "rb") as source, unpacked.open("wb") as handle:
        os.chmod(unpacked, 0o600)
        while chunk := source.read(8 * 1024**2):
            handle.write(chunk)
    return unpacked


def _image_archive_names(archive: Path) -> frozenset[str]:
    try:
        with tarfile.open(archive, mode="r:*") as handle:
            return frozenset(
                member.name.split("/", 1)[0] for member in handle.getmembers()
            )
    except tarfile.TarError as error:
        raise TargonPromoteError(
            "runtime archive is not a readable image tar"
        ) from error


def _image_archive_sources(archive: Path) -> tuple[str, ...]:
    names = _image_archive_names(archive)
    docker = f"docker-archive:{archive}"
    oci = f"oci-archive:{archive}"
    if "oci-layout" in names or "index.json" in names:
        return (oci, docker)
    if "manifest.json" in names:
        return (docker, oci)
    raise TargonPromoteError("runtime archive has neither manifest.json nor oci-layout")


def _skopeo_detail(error: BaseException) -> str:
    text = " ".join(
        part.strip()
        for part in (getattr(error, "stderr", None), getattr(error, "stdout", None))
        if isinstance(part, str) and part.strip()
    )
    if not text:
        return type(error).__name__
    text = re.sub(r"ya29\.[A-Za-z0-9._-]+", "[oauth]", text)
    text = re.sub(r"eyJ[A-Za-z0-9._-]{20,}", "[jwt]", text)
    return " ".join(text.split())[:480]


def _run_skopeo(
    args: list[str], *, registry_host: str, access_token: str, timeout: int
) -> subprocess.CompletedProcess[str]:
    env = _skopeo_env(registry_host=registry_host, access_token=access_token)
    authfile = env["REGISTRY_AUTH_FILE"]
    command = list(args)
    if len(command) > 1 and command[1] == "copy":
        command[2:2] = ["--dest-authfile", authfile]
    elif len(command) > 1 and command[1] == "inspect":
        command[2:2] = ["--authfile", authfile]
    try:
        return subprocess.run(
            command,
            capture_output=True,
            text=True,
            timeout=timeout,
            env=env,
        )
    finally:
        shutil.rmtree(env["DOCKER_CONFIG"], ignore_errors=True)


def _skopeo_env(*, registry_host: str, access_token: str) -> dict[str, str]:
    config_dir = Path(tempfile.mkdtemp(prefix="ditto-skopeo-config-"))
    (config_dir / "config.json").write_text("{}", encoding="utf-8")
    os.chmod(config_dir / "config.json", 0o600)
    encoded = base64.b64encode(f"oauth2accesstoken:{access_token}".encode()).decode()
    auth_path = config_dir / "auth.json"
    auth_path.write_text(
        json.dumps(
            {"auths": {registry_host: {"auth": encoded}}}, separators=(",", ":")
        ),
        encoding="utf-8",
    )
    os.chmod(auth_path, 0o600)
    containers_dir = config_dir / ".config" / "containers" / "registries.conf.d"
    containers_dir.mkdir(parents=True)
    runtime_dir = config_dir / "run"
    runtime_dir.mkdir()
    env = os.environ.copy()
    env["HOME"] = str(config_dir)
    env["XDG_CONFIG_HOME"] = str(config_dir / ".config")
    env["XDG_RUNTIME_DIR"] = str(runtime_dir)
    env["DOCKER_CONFIG"] = str(config_dir)
    env["REGISTRY_AUTH_FILE"] = str(auth_path)
    return env
