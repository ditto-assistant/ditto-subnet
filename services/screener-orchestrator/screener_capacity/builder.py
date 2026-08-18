"""Dedicated Targon-first trusted-image build worker."""

from __future__ import annotations

import argparse
import base64
import contextlib
import fcntl
import gzip
import hashlib
import json
import os
import re
import shlex
import subprocess
import sys
import tarfile
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from screener_capacity.controller import (
    ControllerError,
    GCPBootstrapTokenMinter,
    _read_secret_file,
)
from screener_capacity.oneshot import delete_oneshot_rental, sweep_oneshot_rentals
from screener_capacity.targon import TargonAPIError, TargonClient

_DIGEST = re.compile(r"DITTO_BUILD_DIGEST=(sha256:[0-9a-f]{64})")
_SUBMISSION_EXIT_CODE = re.compile(r"(?:^|\D)exit code (71|72|73|74|75|76)(?:\D|$)")
_SUBMISSION_STAGE_BY_EXIT_CODE = {
    "71": "SOURCE",
    "72": "KANIKO",
    "73": "ARCHIVE",
    "74": "UPLOAD",
    "75": "COMPLETE",
    "76": "CONTRACT",
}
_SUBMISSION_BUILDER_REPOSITORY = (
    "us-central1-docker.pkg.dev/ditto-app-dev/ditto-public-builders/submission-builder"
)
_SOURCE_REVIEW_CLEANUP_GRACE_SECONDS = 5


def _request(
    method: str,
    url: str,
    *,
    token: str,
    payload: dict[str, Any] | None = None,
    retryable: bool = False,
) -> dict[str, Any]:
    request = urllib.request.Request(
        url,
        data=(
            json.dumps(payload, separators=(",", ":")).encode()
            if payload is not None
            else None
        ),
        headers={
            "Accept": "application/json",
            "Authorization": f"Bearer {token}",
            **({"Content-Type": "application/json"} if payload is not None else {}),
        },
        method=method,
    )
    attempts = 3 if retryable else 1
    for attempt in range(attempts):
        try:
            with urllib.request.urlopen(request, timeout=30) as response:
                body = response.read()
            break
        except urllib.error.HTTPError as error:
            transient = error.code == 429 or error.code >= 500
            if transient and attempt + 1 < attempts:
                time.sleep(0.5 * (2**attempt))
                continue
            raise ControllerError(
                f"Platform trusted-build {method} failed with HTTP {error.code}"
            ) from None
        except (TimeoutError, urllib.error.URLError, OSError) as error:
            if attempt + 1 < attempts:
                time.sleep(0.5 * (2**attempt))
                continue
            raise ControllerError("Platform trusted-build transport failed") from error
    try:
        value = json.loads(body) if body else {}
    except json.JSONDecodeError as error:
        raise ControllerError("Platform trusted-build response is invalid") from error
    if not isinstance(value, dict):
        raise ControllerError("Platform trusted-build response has invalid shape")
    return value


class BuildControl:
    def __init__(self, *, platform_url: str, token: str, environment: str, epoch: str):
        self.base = platform_url.rstrip("/")
        self.token = token
        self.environment = environment
        self.epoch = epoch

    def claim(self) -> dict[str, Any] | None:
        value = _request(
            "POST",
            f"{self.base}/api/v1/screener/controller/trusted-image-builds/claim",
            token=self.token,
            payload={"environment": self.environment, "controller_epoch": self.epoch},
        )
        build = value.get("build")
        if build is None:
            return None
        if not isinstance(build, dict):
            raise ControllerError("Platform trusted-build claim is invalid")
        return build

    def update(
        self,
        build_id: str,
        *,
        status: str,
        provider_resource_id: str | None = None,
        image_digest: str | None = None,
        error_code: str | None = None,
    ) -> None:
        _request(
            "PUT",
            f"{self.base}/api/v1/screener/controller/trusted-image-builds/{build_id}",
            token=self.token,
            payload={
                "environment": self.environment,
                "controller_epoch": self.epoch,
                "status": status,
                "provider": "targon",
                "provider_resource_id": provider_resource_id,
                "image_digest": image_digest,
                "error_code": error_code,
            },
        )

    def cleanup_required(self, build_id: str, *, provider_resource_id: str) -> None:
        _request(
            "POST",
            f"{self.base}/api/v1/screener/controller/"
            f"trusted-image-builds/{build_id}/cleanup-required",
            token=self.token,
            payload={
                "environment": self.environment,
                "controller_epoch": self.epoch,
                "provider_resource_id": provider_resource_id,
            },
        )


class SubmissionBuildControl:
    def __init__(self, *, platform_url: str, token: str, environment: str, epoch: str):
        self.base = platform_url.rstrip("/")
        self.token = token
        self.environment = environment
        self.epoch = epoch

    def claim(self) -> dict[str, Any] | None:
        value = _request(
            "POST",
            f"{self.base}/api/v1/screener/controller/submission-image-builds/claim",
            token=self.token,
            payload={"environment": self.environment, "controller_epoch": self.epoch},
        )
        build = value.get("build")
        if build is None:
            return None
        if not isinstance(build, dict):
            raise ControllerError("Platform submission-build claim is invalid")
        return build

    def update(
        self,
        build_id: str,
        *,
        status: str,
        provider_resource_id: str | None = None,
        error_code: str | None = None,
    ) -> None:
        try:
            _request(
                "PUT",
                f"{self.base}/api/v1/screener/controller/"
                f"submission-image-builds/{build_id}",
                token=self.token,
                payload={
                    "environment": self.environment,
                    "controller_epoch": self.epoch,
                    "status": status,
                    "provider_resource_id": provider_resource_id,
                    "error_code": error_code,
                },
                retryable=True,
            )
        except ControllerError:
            # A proxy may lose the response after Platform committed the
            # idempotent transition. Re-read before turning that ambiguity into
            # a false provider failure.
            if self.status(build_id) != status:
                raise

    def status(self, build_id: str) -> str:
        query = urllib.parse.urlencode(
            {"environment": self.environment, "controller_epoch": self.epoch}
        )
        value = _request(
            "GET",
            f"{self.base}/api/v1/screener/controller/"
            f"submission-image-builds/{build_id}?{query}",
            token=self.token,
            retryable=True,
        )
        status = value.get("status")
        if not isinstance(status, str):
            raise ControllerError("Platform submission-build status is invalid")
        return status

    def cleanup_required(self, build_id: str, *, provider_resource_id: str) -> None:
        _request(
            "POST",
            f"{self.base}/api/v1/screener/controller/"
            f"submission-image-builds/{build_id}/cleanup-required",
            token=self.token,
            payload={
                "environment": self.environment,
                "controller_epoch": self.epoch,
                "provider_resource_id": provider_resource_id,
            },
        )


class SourceReviewControl:
    def __init__(self, *, platform_url: str, token: str, environment: str, epoch: str):
        self.base = platform_url.rstrip("/")
        self.token = token
        self.environment = environment
        self.epoch = epoch

    def claim(self) -> dict[str, Any] | None:
        value = _request(
            "POST",
            f"{self.base}/api/v1/screener/controller/submission-source-reviews/claim",
            token=self.token,
            payload={"environment": self.environment, "controller_epoch": self.epoch},
        )
        review = value.get("review")
        if review is None:
            return None
        if not isinstance(review, dict):
            raise ControllerError("Platform source-review claim is invalid")
        return review

    def update(
        self,
        review_id: str,
        *,
        status: str,
        provider_resource_id: str | None = None,
        error_code: str | None = None,
    ) -> None:
        _request(
            "PUT",
            f"{self.base}/api/v1/screener/controller/submission-source-reviews/{review_id}",
            token=self.token,
            payload={
                "environment": self.environment,
                "controller_epoch": self.epoch,
                "status": status,
                "provider_resource_id": provider_resource_id,
                "error_code": error_code,
            },
            retryable=True,
        )

    def status(self, review_id: str) -> str:
        query = urllib.parse.urlencode(
            {"environment": self.environment, "controller_epoch": self.epoch}
        )
        value = _request(
            "GET",
            f"{self.base}/api/v1/screener/controller/submission-source-reviews/"
            f"{review_id}?{query}",
            token=self.token,
            retryable=True,
        )
        status = value.get("status")
        if not isinstance(status, str):
            raise ControllerError("Platform source-review status is invalid")
        return status

    def cleanup_required(self, review_id: str, *, provider_resource_id: str) -> None:
        _request(
            "POST",
            f"{self.base}/api/v1/screener/controller/submission-source-reviews/"
            f"{review_id}/cleanup-required",
            token=self.token,
            payload={
                "environment": self.environment,
                "controller_epoch": self.epoch,
                "provider_resource_id": provider_resource_id,
            },
        )


class RuntimeSmokeControl:
    def __init__(self, *, platform_url: str, token: str, environment: str, epoch: str):
        self.base = platform_url.rstrip("/")
        self.token = token
        self.environment = environment
        self.epoch = epoch

    def claim(self) -> dict[str, Any] | None:
        value = _request(
            "POST",
            f"{self.base}/api/v1/screener/controller/submission-runtime-smokes/claim",
            token=self.token,
            payload={"environment": self.environment, "controller_epoch": self.epoch},
        )
        artifact = value.get("artifact")
        if artifact is None:
            return None
        if not isinstance(artifact, dict):
            raise ControllerError("Platform runtime-smoke claim is invalid")
        return artifact

    def update(
        self,
        build_id: str,
        *,
        status: str,
        provider_resource_id: str | None = None,
        image_reference: str | None = None,
        error_code: str | None = None,
    ) -> None:
        _request(
            "POST",
            f"{self.base}/api/v1/screener/controller/submission-image-builds/"
            f"{build_id}/runtime-result",
            token=self.token,
            payload={
                "environment": self.environment,
                "controller_epoch": self.epoch,
                "status": status,
                "provider_resource_id": provider_resource_id,
                "image_reference": image_reference,
                "error_code": error_code,
            },
            retryable=True,
        )

    def cleanup_required(self, build_id: str, *, provider_resource_id: str) -> None:
        _request(
            "POST",
            f"{self.base}/api/v1/screener/controller/submission-image-builds/"
            f"{build_id}/runtime-cleanup-required",
            token=self.token,
            payload={
                "environment": self.environment,
                "controller_epoch": self.epoch,
                "provider_resource_id": provider_resource_id,
            },
        )


def _mint_access_token(service_account: str) -> str:
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
            timeout=30,
        )
    except (OSError, subprocess.SubprocessError) as error:
        raise ControllerError("registry token mint failed") from error
    token = result.stdout.strip()
    if len(token) < 100:
        raise ControllerError("registry token mint returned invalid data")
    return token


def _docker_config(destination: str, access_token: str) -> str:
    host = destination.split("/", 1)[0]
    config = {
        "auths": {host: {"username": "oauth2accesstoken", "password": access_token}}
    }
    return base64.b64encode(json.dumps(config, separators=(",", ":")).encode()).decode()


def _submission_builder_image(source_sha: str) -> str:
    if not re.fullmatch(r"[0-9a-f]{40}", source_sha):
        raise ControllerError("submission builder source revision is invalid")
    image = f"{_SUBMISSION_BUILDER_REPOSITORY}:sha-{source_sha}"
    try:
        result = subprocess.run(
            [
                "gcloud",
                "artifacts",
                "docker",
                "images",
                "describe",
                image,
                "--format=value(image_summary.digest)",
                "--quiet",
            ],
            check=True,
            capture_output=True,
            text=True,
            timeout=30,
        )
    except (OSError, subprocess.SubprocessError) as error:
        raise ControllerError("submission builder image resolution failed") from error
    digest = result.stdout.strip()
    if not re.fullmatch(r"sha256:[0-9a-f]{64}", digest):
        raise ControllerError("submission builder image digest is invalid")
    return f"{_SUBMISSION_BUILDER_REPOSITORY}@{digest}"


def _source_revision() -> str:
    try:
        result = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            check=True,
            capture_output=True,
            text=True,
            timeout=10,
        )
    except (OSError, subprocess.SubprocessError) as error:
        raise ControllerError("builder source revision unavailable") from error
    revision = result.stdout.strip()
    if not re.fullmatch(r"[0-9a-f]{40}", revision):
        raise ControllerError("builder source revision is invalid")
    return revision


def _kaniko_script(build: dict[str, Any]) -> str:
    repository = str(build["source_repository"]).removesuffix(".git")
    sha = str(build["source_sha"])
    archive = shlex.quote(f"{repository}/archive/{sha}.tar.gz")
    checkout = shlex.quote(f"/workspace/src/ditto-subnet-{sha}")
    destination = str(build["destination"])
    dockerfile = str(build["dockerfile_path"])
    cache_repo = destination.rsplit("/", 1)[0] + "/cache"
    return (
        "set -eu; umask 077; mkdir -p /kaniko/.docker /workspace/src; "
        "printf '%s' \"$DITTO_DOCKER_CONFIG_B64\" | /busybox/base64 -d "
        ">/kaniko/.docker/config.json; "
        "unset DITTO_DOCKER_CONFIG_B64; "
        f"/busybox/wget -qO /workspace/source.tar.gz {archive}; "
        "/busybox/tar -xzf /workspace/source.tar.gz -C /workspace/src; "
        "/busybox/rm /workspace/source.tar.gz; "
        f"/kaniko/executor --context={checkout} --dockerfile={shlex.quote(dockerfile)} "
        f"--destination={shlex.quote(destination)} --digest-file=/workspace/digest "
        f"--cache=true --cache-repo={shlex.quote(cache_repo)} --verbosity=info; "
        "digest=$(/busybox/cat /workspace/digest); "
        'case "$digest" in '
        "sha256:????????????????????????????????????????????????????????????????) ;; "
        "*) exit 71 ;; esac; "
        "echo DITTO_BUILD_DIGEST=$digest; sleep 1800"
    )


def _delete_rental(client: TargonClient, uid: str) -> bool:
    return delete_oneshot_rental(client, uid)


@dataclass(frozen=True)
class Settings:
    environment: str
    epoch: str
    targon_resource: str
    kaniko_image: str
    registry_service_account: str
    provision_timeout_seconds: int
    build_timeout_seconds: int
    interval_seconds: int
    lock_file: Path
    sweep_interval_seconds: int
    submission_builder_image: str
    # The four fields below are optional ON PURPOSE. They are supplied by the
    # Ansible-owned systemd unit, while the code is shipped by the release
    # deploy -- two paths that are not synchronized. When they were required
    # argparse arguments, a release carrying new code reached a host whose unit
    # predated it, the builder exited 2/INVALIDARGUMENT on every restart, the
    # deploy healthcheck failed, and the whole controller rolled back. Absent
    # configuration must disable the lane that needs it, never the daemon.
    gcp_bootstrap_service_account: str | None
    gcp_bootstrap_delegate_service_account: str | None
    source_review_secret_resource: str | None
    source_review_timeout_seconds: int
    candidate_registry_service_account: str | None
    candidate_registry_writer_service_account: str | None
    runtime_timeout_seconds: int

    @property
    def runtime_smoke_enabled(self) -> bool:
        """Runtime smoke needs both candidate-registry identities to pull/push."""
        return bool(
            self.candidate_registry_service_account
            and self.candidate_registry_writer_service_account
        )

    @property
    def source_review_enabled(self) -> bool:
        """Source review needs the bootstrap identity and its secret resource."""
        return bool(
            self.gcp_bootstrap_service_account and self.source_review_secret_resource
        )

    def disabled_lanes(self) -> list[str]:
        """Lane names skipped for missing configuration, for a startup log."""
        missing = []
        if not self.runtime_smoke_enabled:
            missing.append("runtime-smoke")
        if not self.source_review_enabled:
            missing.append("source-review")
        return missing


def _download_runtime_archive(artifact: dict[str, Any], destination: Path) -> None:
    try:
        url = base64.b64decode(str(artifact["archive_url_b64"]), validate=True).decode()
        expected = str(artifact["output_sha256"])
        expected_size = int(artifact["output_size_bytes"])
    except (KeyError, TypeError, ValueError, UnicodeError) as error:
        raise ControllerError("runtime artifact contract is invalid") from error
    if not url.startswith("https://") or not re.fullmatch(r"[0-9a-f]{64}", expected):
        raise ControllerError("runtime artifact contract is invalid")
    digest = hashlib.sha256()
    total = 0
    try:
        with (
            urllib.request.urlopen(url, timeout=300) as response,
            destination.open("xb") as handle,
        ):
            os.chmod(destination, 0o600)
            while chunk := response.read(8 * 1024**2):
                total += len(chunk)
                if total > expected_size:
                    raise ControllerError("runtime artifact exceeded declared size")
                digest.update(chunk)
                handle.write(chunk)
    except (OSError, urllib.error.URLError) as error:
        raise ControllerError("runtime artifact download failed") from error
    if total != expected_size or digest.hexdigest() != expected:
        raise ControllerError("runtime artifact verification failed")


def _materialize_image_archive(archive: Path) -> Path:
    """Kaniko may emit gzip; skopeo's archive transports want a plain tar."""
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
        raise ControllerError("runtime archive is not a readable image tar") from error


def _image_archive_sources(archive: Path) -> tuple[str, ...]:
    names = _image_archive_names(archive)
    docker = f"docker-archive:{archive}"
    oci = f"oci-archive:{archive}"
    if "oci-layout" in names or "index.json" in names:
        return (oci, docker)
    if "manifest.json" in names:
        return (docker, oci)
    raise ControllerError("runtime archive has neither manifest.json nor oci-layout")


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


def _skopeo_env(*, registry_host: str, access_token: str) -> dict[str, str]:
    # Host skopeo is 1.18 and has no --dest-password-stdin. Write an isolated
    # authfile instead. Empty DOCKER_CONFIG so ProtectHome cannot pull in
    # Docker credHelpers for *.pkg.dev.
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
    env = os.environ.copy()
    env["DOCKER_CONFIG"] = str(config_dir)
    env["REGISTRY_AUTH_FILE"] = str(auth_path)
    return env


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
        config_dir = Path(env["DOCKER_CONFIG"])
        for child in config_dir.glob("*"):
            child.unlink(missing_ok=True)
        config_dir.rmdir()


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
                [
                    "skopeo",
                    "copy",
                    source,
                    f"docker://{destination}",
                ],
                registry_host=registry_host,
                access_token=access_token,
                timeout=600,
            )
            if result.returncode == 0:
                copied = True
                break
            last_error = subprocess.CalledProcessError(
                result.returncode, result.args, result.stdout, result.stderr
            )
        if not copied:
            assert last_error is not None
            raise ControllerError(
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
            timeout=60,
        )
        if inspect.returncode != 0:
            inspect_error = subprocess.CalledProcessError(
                inspect.returncode, inspect.args, inspect.stdout, inspect.stderr
            )
            raise ControllerError(
                f"runtime image promotion failed: {_skopeo_detail(inspect_error)}"
            ) from inspect_error
    except ControllerError:
        raise
    except (OSError, subprocess.SubprocessError) as error:
        raise ControllerError(
            f"runtime image promotion failed: {_skopeo_detail(error)}"
        ) from error
    finally:
        if unpacked is not None and unpacked != archive:
            unpacked.unlink(missing_ok=True)
    digest = inspect.stdout.strip()
    if not re.fullmatch(r"sha256:[0-9a-f]{64}", digest):
        raise ControllerError("runtime image promotion returned invalid digest")
    return f"{destination.rsplit(':', 1)[0]}@{digest}"


def run_one_runtime_smoke(
    settings: Settings,
    client: TargonClient,
    control: RuntimeSmokeControl,
) -> bool:
    # Fail closed rather than claiming work this builder cannot finish: an
    # unconfigured lane must not take a lease it would only abandon.
    writer = settings.candidate_registry_writer_service_account
    reader = settings.candidate_registry_service_account
    if not writer or not reader:
        raise ControllerError("runtime smoke lane is not configured")
    artifact = control.claim()
    if artifact is None:
        return False
    build_id = str(artifact.get("build_id", ""))
    uid: str | None = None
    archive: Path | None = None
    try:
        inventory = {
            str(row.get("name")): int(row.get("available", 0))
            for row in client.inventory()
        }
        if inventory.get(settings.targon_resource, 0) < 1:
            control.update(
                build_id,
                status="fallback_required",
                error_code="TARGON_RUNTIME_CAPACITY_UNAVAILABLE",
            )
            return True
        destination = str(artifact["destination"])
        descriptor, raw_path = tempfile.mkstemp(prefix="ditto-runtime-", suffix=".tar")
        os.close(descriptor)
        os.unlink(raw_path)
        archive = Path(raw_path)
        _download_runtime_archive(artifact, archive)
        promotion_token = _mint_access_token(writer)
        image_reference = _promote_runtime_archive(
            archive=archive, destination=destination, access_token=promotion_token
        )
        pull_token = _mint_access_token(reader)
        created = client.create_rental(
            name=f"ditto-runtime-{build_id.replace('-', '')[:14]}"[:32],
            image=image_reference,
            resource_name=settings.targon_resource,
            envs=[
                {"name": "OPENROUTER_API_KEY", "value": "sk-screener-smoke"},
                {"name": "DITTOBENCH_DB", "value": "/tmp/dittobench.db"},
            ],
            ports=[{"port": 8080, "protocol": "TCP", "routing": "PROXIED"}],
            registry_auth={
                "server": destination.split("/", 1)[0],
                "username": "oauth2accesstoken",
                "password": pull_token,
            },
        )
        uid = str(created["uid"])
        control.update(build_id, status="running", provider_resource_id=uid)
        client.deploy(uid)
        deadline = time.monotonic() + settings.runtime_timeout_seconds
        while time.monotonic() < deadline:
            state = client.state(uid)
            urls = state.get("urls")
            if isinstance(urls, list):
                for row in urls:
                    if not isinstance(row, dict) or row.get("port") != 8080:
                        continue
                    url = str(row.get("url", "")).rstrip("/")
                    if not url.startswith("https://"):
                        continue
                    try:
                        with urllib.request.urlopen(
                            f"{url}/health", timeout=5
                        ) as response:
                            if 200 <= response.status < 300:
                                control.update(
                                    build_id,
                                    status="succeeded",
                                    provider_resource_id=uid,
                                    image_reference=image_reference,
                                )
                                return True
                    except (OSError, urllib.error.URLError):
                        pass
            if str(state.get("status", "")).casefold() == "error":
                break
            time.sleep(5)
        control.update(
            build_id,
            status="fallback_required",
            provider_resource_id=uid,
            error_code="TARGON_RUNTIME_HEALTH_FAILED",
        )
        return True
    except (ControllerError, TargonAPIError, KeyError, ValueError) as error:
        print(f"runtime smoke failed: {type(error).__name__}: {error}", file=sys.stderr)
        with contextlib.suppress(ControllerError):
            control.update(
                build_id,
                status="fallback_required",
                provider_resource_id=uid,
                error_code="TARGON_RUNTIME_PROVIDER_ERROR",
            )
        return True
    finally:
        if archive is not None:
            archive.unlink(missing_ok=True)
        if uid is not None and not _delete_rental(client, uid):
            with contextlib.suppress(ControllerError):
                control.cleanup_required(build_id, provider_resource_id=uid)


def run_one_source_review(
    settings: Settings,
    client: TargonClient,
    control: SourceReviewControl,
) -> bool:
    # Fail closed rather than claiming work this builder cannot finish.
    bootstrap_target = settings.gcp_bootstrap_service_account
    review_secret = settings.source_review_secret_resource
    if not bootstrap_target or not review_secret:
        raise ControllerError("source review lane is not configured")
    review = control.claim()
    if review is None:
        return False
    review_id = str(review.get("review_id", ""))
    name = f"ditto-source-{review_id.replace('-', '')[:16]}"[:32]
    uid: str | None = None
    try:
        inventory = {
            str(row.get("name")): int(row.get("available", 0))
            for row in client.inventory()
        }
        if inventory.get(settings.targon_resource, 0) < 1:
            control.update(
                review_id,
                status="fallback_required",
                error_code="TARGON_SOURCE_REVIEW_CAPACITY_UNAVAILABLE",
            )
            return True
        image_reference = str(review["image_reference"])
        if (
            re.fullmatch(
                r"[a-z0-9.-]+(?::[0-9]+)?/[a-z0-9._/-]+@sha256:[0-9a-f]{64}",
                image_reference,
            )
            is None
        ):
            raise ValueError("source-review image reference is invalid")
        job_token = str(review["job_token"])
        if len(job_token) < 43:
            raise ValueError("source-review token is invalid")
        bootstrap_token = GCPBootstrapTokenMinter(
            target=bootstrap_target,
            delegate=settings.gcp_bootstrap_delegate_service_account,
        ).mint()
        env = {
            "DITTO_PLATFORM_URL": control.base,
            "DITTO_SOURCE_REVIEW_ID": review_id,
            "DITTO_SOURCE_REVIEW_ATTEMPT_ID": str(review["attempt_id"]),
            "DITTO_SOURCE_REVIEW_ARTIFACT_SHA256": str(review["artifact_sha256"]),
            "DITTO_SOURCE_REVIEW_JOB_TOKEN": job_token,
            "DITTO_SOURCE_REVIEW_JOB": "1",
            "SCREENER_NODE_CREDENTIAL_FILE": "/tmp/ditto-source-review/node.json",
            "SCREENER_GCP_BOOTSTRAP_ACCESS_TOKEN": bootstrap_token,
            "SCREENER_SOURCE_REVIEW_SECRET_RESOURCE": review_secret,
            "SCREENER_SOURCE_REVIEW_TIMEOUT_SECONDS": str(
                settings.source_review_timeout_seconds
            ),
            "SCREENER_STATIC_PREFLIGHT_V2_MODE": "off",
        }
        created = client.create_rental(
            name=name,
            image=image_reference,
            resource_name=settings.targon_resource,
            envs=[{"name": key, "value": value} for key, value in sorted(env.items())],
            commands=["/app/workers/screener/.venv/bin/python", "-m"],
            args=["ditto_screener.source_review_job"],
        )
        uid = str(created["uid"])
        control.update(review_id, status="running", provider_resource_id=uid)
        client.deploy(uid)
        deadline = (
            time.monotonic()
            + settings.provision_timeout_seconds
            + (settings.source_review_timeout_seconds + 120)
        )
        while time.monotonic() < deadline:
            status = control.status(review_id)
            if status in {"succeeded", "fallback_required", "canceled", "consumed"}:
                # Platform commits completion just before the job receives the
                # HTTP response and enters its hold. Give that process a small
                # bounded window before DELETE so Targon does not turn the
                # termination into an exit-143 provider error.
                time.sleep(_SOURCE_REVIEW_CLEANUP_GRACE_SECONDS)
                return True
            state = client.state(uid)
            if str(state.get("status", "")).casefold() == "error":
                control.update(
                    review_id,
                    status="fallback_required",
                    provider_resource_id=uid,
                    error_code="TARGON_SOURCE_REVIEW_RUNTIME_ERROR",
                )
                return True
            time.sleep(10)
        control.update(
            review_id,
            status="fallback_required",
            provider_resource_id=uid,
            error_code="TARGON_SOURCE_REVIEW_TIMEOUT",
        )
        return True
    except (ControllerError, TargonAPIError, KeyError, ValueError):
        with contextlib.suppress(ControllerError):
            control.update(
                review_id,
                status="fallback_required",
                provider_resource_id=uid,
                error_code="TARGON_SOURCE_REVIEW_PROVIDER_ERROR",
            )
        return True
    finally:
        if uid is not None and not _delete_rental(client, uid):
            with contextlib.suppress(ControllerError):
                control.cleanup_required(review_id, provider_resource_id=uid)


def run_one(settings: Settings, client: TargonClient, control: BuildControl) -> bool:
    build = control.claim()
    if build is None:
        return False
    build_id = str(build.get("build_id", ""))
    name = f"ditto-build-{build_id.replace('-', '')[:16]}"[:32]
    uid: str | None = None
    try:
        inventory = {
            str(row.get("name")): int(row.get("available", 0))
            for row in client.inventory()
        }
        if inventory.get(settings.targon_resource, 0) < 1:
            control.update(
                build_id,
                status="fallback_required",
                error_code="TARGON_BUILD_CAPACITY_UNAVAILABLE",
            )
            return True
        registry_token = _mint_access_token(settings.registry_service_account)
        created = client.create_rental(
            name=name,
            image=settings.kaniko_image,
            resource_name=settings.targon_resource,
            envs=[
                {
                    "name": "DITTO_DOCKER_CONFIG_B64",
                    "value": _docker_config(str(build["destination"]), registry_token),
                }
            ],
            commands=["/busybox/sh", "-c"],
            args=[_kaniko_script(build)],
        )
        uid = str(created["uid"])
        control.update(build_id, status="running", provider_resource_id=uid)
        client.deploy(uid)
        deadline = (
            time.monotonic()
            + settings.provision_timeout_seconds
            + settings.build_timeout_seconds
        )
        while time.monotonic() < deadline:
            state = client.state(uid)
            logs = client.logs(uid, tail=160)
            match = _DIGEST.search(logs)
            if match is not None:
                control.update(
                    build_id,
                    status="succeeded",
                    provider_resource_id=uid,
                    image_digest=match.group(1),
                )
                return True
            if str(state.get("status", "")).casefold() == "error":
                break
            time.sleep(10)
        control.update(
            build_id,
            status="fallback_required",
            provider_resource_id=uid,
            error_code="TARGON_KANIKO_BUILD_FAILED",
        )
        return True
    except (ControllerError, TargonAPIError, KeyError, ValueError):
        with contextlib.suppress(ControllerError):
            control.update(
                build_id,
                status="fallback_required",
                provider_resource_id=uid,
                error_code="TARGON_KANIKO_PROVIDER_ERROR",
            )
        return True
    finally:
        if uid is not None and not _delete_rental(client, uid):
            with contextlib.suppress(ControllerError):
                control.cleanup_required(build_id, provider_resource_id=uid)


def run_one_submission(
    settings: Settings,
    client: TargonClient,
    control: SubmissionBuildControl,
) -> bool:
    build = control.claim()
    if build is None:
        return False
    build_id = str(build.get("build_id", ""))
    name = f"ditto-miner-build-{build_id.replace('-', '')[:12]}"[:32]
    uid: str | None = None
    phase = "inventory"
    try:
        inventory = {
            str(row.get("name")): int(row.get("available", 0))
            for row in client.inventory()
        }
        if inventory.get(settings.targon_resource, 0) < 1:
            control.update(
                build_id,
                status="fallback_required",
                error_code="TARGON_SUBMISSION_BUILD_CAPACITY_UNAVAILABLE",
            )
            return True
        job_token = str(build["job_token"])
        if len(job_token) < 43:
            raise ValueError("submission build token is invalid")
        phase = "create"
        created = client.create_rental(
            name=name,
            image=settings.submission_builder_image,
            resource_name=settings.targon_resource,
            envs=[
                {"name": "DITTO_PLATFORM_URL", "value": control.base},
                {"name": "DITTO_BUILD_ID", "value": build_id},
                {"name": "DITTO_BUILD_JOB_TOKEN", "value": job_token},
            ],
        )
        uid = str(created["uid"])
        phase = "mark_running"
        control.update(build_id, status="running", provider_resource_id=uid)
        phase = "deploy"
        client.deploy(uid)
        deadline = (
            time.monotonic()
            + settings.provision_timeout_seconds
            + settings.build_timeout_seconds
        )
        provider_poll_failures = 0
        platform_poll_failures = 0
        phase = "monitor"
        while time.monotonic() < deadline:
            try:
                state = client.state(uid)
                provider_poll_failures = 0
            except TargonAPIError:
                provider_poll_failures += 1
                if provider_poll_failures < 3:
                    time.sleep(10)
                    continue
                raise
            try:
                build_status = control.status(build_id)
                platform_poll_failures = 0
            except ControllerError:
                platform_poll_failures += 1
                if platform_poll_failures < 3:
                    time.sleep(10)
                    continue
                raise
            if build_status == "succeeded":
                return True
            if build_status in {"fallback_required", "canceled", "consumed"}:
                return True
            if str(state.get("status", "")).casefold() == "error":
                error_code = "TARGON_SUBMISSION_RUNTIME_ERROR"
                match = _SUBMISSION_EXIT_CODE.search(str(state.get("message", "")))
                if match is not None:
                    stage = _SUBMISSION_STAGE_BY_EXIT_CODE[match.group(1)]
                    error_code = f"TARGON_SUBMISSION_{stage}_FAILED"
                control.update(
                    build_id,
                    status="fallback_required",
                    provider_resource_id=uid,
                    error_code=error_code,
                )
                return True
            time.sleep(10)
        control.update(
            build_id,
            status="fallback_required",
            provider_resource_id=uid,
            error_code="TARGON_SUBMISSION_BUILD_TIMEOUT",
        )
        return True
    except TargonAPIError:
        error_code = (
            "TARGON_SUBMISSION_DEPLOY_ERROR"
            if phase == "deploy"
            else "TARGON_SUBMISSION_PROVIDER_ERROR"
        )
        with contextlib.suppress(ControllerError):
            control.update(
                build_id,
                status="fallback_required",
                provider_resource_id=uid,
                error_code=error_code,
            )
        return True
    except ControllerError:
        with contextlib.suppress(ControllerError):
            control.update(
                build_id,
                status="fallback_required",
                provider_resource_id=uid,
                error_code="TARGON_SUBMISSION_PLATFORM_CONTROL_ERROR",
            )
        return True
    except (KeyError, ValueError):
        with contextlib.suppress(ControllerError):
            control.update(
                build_id,
                status="fallback_required",
                provider_resource_id=uid,
                error_code="TARGON_SUBMISSION_CONTRACT_ERROR",
            )
        return True
    finally:
        if uid is not None and not _delete_rental(client, uid):
            with contextlib.suppress(ControllerError):
                control.cleanup_required(build_id, provider_resource_id=uid)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser()
    parser.add_argument("--platform-url", required=True)
    parser.add_argument("--platform-token-file", required=True)
    parser.add_argument("--environment", default="prod")
    parser.add_argument("--controller-epoch")
    parser.add_argument("--targon-api-key-file", required=True)
    parser.add_argument("--targon-org-slug", required=True)
    parser.add_argument("--targon-resource", default="cpu-small")
    parser.add_argument("--kaniko-image", required=True)
    parser.add_argument("--registry-service-account", required=True)
    # Deliberately NOT required: see Settings. A missing value disables exactly
    # the lane that needs it so a code-only deploy degrades instead of
    # crash-looping the daemon and rolling the controller back.
    parser.add_argument("--gcp-bootstrap-service-account")
    parser.add_argument("--gcp-bootstrap-delegate-service-account")
    parser.add_argument("--source-review-secret-resource")
    parser.add_argument("--source-review-timeout-seconds", type=int, default=180)
    parser.add_argument("--candidate-registry-service-account")
    parser.add_argument("--candidate-registry-writer-service-account")
    parser.add_argument("--runtime-timeout-seconds", type=int, default=180)
    parser.add_argument("--provision-timeout-seconds", type=int, default=600)
    parser.add_argument("--build-timeout-seconds", type=int, default=1800)
    parser.add_argument("--interval-seconds", type=int, default=15)
    parser.add_argument("--sweep-interval-seconds", type=int, default=300)
    parser.add_argument("--lock-file", default="/run/lock/ditto-image-builder.lock")
    parser.add_argument("--once", action="store_true")
    return parser


def main() -> int:
    args = build_parser().parse_args()
    platform_token = _read_secret_file(Path(args.platform_token_file))
    targon_key = _read_secret_file(Path(args.targon_api_key_file))
    epoch = args.controller_epoch or f"builder:{os.uname().nodename}:{os.getpid()}"
    settings = Settings(
        environment=args.environment,
        epoch=epoch,
        targon_resource=args.targon_resource,
        kaniko_image=args.kaniko_image,
        registry_service_account=args.registry_service_account,
        provision_timeout_seconds=args.provision_timeout_seconds,
        build_timeout_seconds=args.build_timeout_seconds,
        interval_seconds=args.interval_seconds,
        lock_file=Path(args.lock_file),
        sweep_interval_seconds=max(0, args.sweep_interval_seconds),
        submission_builder_image=_submission_builder_image(_source_revision()),
        gcp_bootstrap_service_account=args.gcp_bootstrap_service_account,
        gcp_bootstrap_delegate_service_account=(
            args.gcp_bootstrap_delegate_service_account
        ),
        source_review_secret_resource=args.source_review_secret_resource,
        source_review_timeout_seconds=args.source_review_timeout_seconds,
        candidate_registry_service_account=args.candidate_registry_service_account,
        candidate_registry_writer_service_account=(
            args.candidate_registry_writer_service_account
        ),
        runtime_timeout_seconds=args.runtime_timeout_seconds,
    )
    control = BuildControl(
        platform_url=args.platform_url,
        token=platform_token,
        environment=args.environment,
        epoch=epoch,
    )
    submission_control = SubmissionBuildControl(
        platform_url=args.platform_url,
        token=platform_token,
        environment=args.environment,
        epoch=epoch,
    )
    source_review_control = SourceReviewControl(
        platform_url=args.platform_url,
        token=platform_token,
        environment=args.environment,
        epoch=epoch,
    )
    runtime_control = RuntimeSmokeControl(
        platform_url=args.platform_url,
        token=platform_token,
        environment=args.environment,
        epoch=epoch,
    )
    client = TargonClient(api_key=targon_key, org_slug=args.targon_org_slug)
    # Announce a degraded builder loudly and exactly once. Silently skipping a
    # lane would look identical to an idle queue, which is how "source review
    # has never produced a row" could go unnoticed.
    disabled = settings.disabled_lanes()
    if disabled:
        print(
            "trusted image builder starting with disabled lanes: "
            + ", ".join(disabled)
            + " (missing configuration; the systemd unit is Ansible-owned and "
            "may predate this release)",
            file=sys.stderr,
        )
    settings.lock_file.parent.mkdir(parents=True, exist_ok=True)
    with settings.lock_file.open("w") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            print("trusted image builder already running", file=sys.stderr)
            return 75
        next_sweep = 0.0
        while True:
            try:
                # Build first because both downstream Targon lanes consume a
                # successfully verified image. Runtime smoke then qualifies
                # that exact archive before source review uses spare capacity.
                handled = run_one_submission(settings, client, submission_control)
                # Smoke the just-finished archive even when another miner
                # build is already queued. Waiting for an empty Kaniko queue
                # lets GCE consume the archive first.
                if settings.runtime_smoke_enabled:
                    handled = (
                        run_one_runtime_smoke(settings, client, runtime_control)
                        or handled
                    )
                if not handled and settings.source_review_enabled:
                    handled = run_one_source_review(
                        settings, client, source_review_control
                    )
                if not handled:
                    handled = run_one(settings, client, control)
            except ControllerError as error:
                # Claim/read outages are control-plane failures, not a reason to
                # terminate the daemon or mislabel a Targon rental. The next
                # bounded poll retries without exposing response bodies.
                print(str(error), file=sys.stderr)
                handled = False
            now = time.monotonic()
            if handled or now >= next_sweep:
                try:
                    swept = sweep_oneshot_rentals(client)
                    print(
                        json.dumps(
                            {
                                "phase": swept["phase"],
                                "oneshot": swept["oneshot"],
                                "deleted": swept["deleted"],
                                "leftover": swept["leftover"],
                                "skipped_inflight": swept["skipped_inflight"],
                            },
                            separators=(",", ":"),
                        ),
                        file=sys.stderr,
                    )
                except TargonAPIError as error:
                    print(str(error), file=sys.stderr)
                next_sweep = now + settings.sweep_interval_seconds
            if args.once:
                return 0
            if not handled:
                time.sleep(settings.interval_seconds)


if __name__ == "__main__":
    raise SystemExit(main())
