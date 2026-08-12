"""Dedicated Targon-first trusted-image build worker."""

from __future__ import annotations

import argparse
import base64
import contextlib
import fcntl
import json
import os
import re
import shlex
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from screener_capacity.controller import ControllerError, _read_secret_file
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
        "echo DITTO_BUILD_DIGEST=$digest; sleep 600"
    )


def _delete_rental(client: TargonClient, uid: str) -> bool:
    """Delete first; suspend only as the zero-replica failure fallback.

    Targon's DELETE contract tears down the runtime itself. Suspending first can
    race that teardown and leave an otherwise successful one-shot build in a
    terminal provider state that can no longer be deleted.
    """
    try:
        client.delete(uid)
        return True
    except TargonAPIError:
        try:
            state = client.state(uid)
        except TargonAPIError:
            state = {}
        if str(state.get("status", "")).casefold() == "deleted":
            return True
        if str(state.get("status", "")).casefold() != "suspended":
            with contextlib.suppress(TargonAPIError):
                client.suspend(uid)
        return False


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
    submission_builder_image: str


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
        if uid is not None:
            _delete_rental(client, uid)


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
    parser.add_argument("--provision-timeout-seconds", type=int, default=600)
    parser.add_argument("--build-timeout-seconds", type=int, default=1800)
    parser.add_argument("--interval-seconds", type=int, default=15)
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
        submission_builder_image=_submission_builder_image(_source_revision()),
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
    client = TargonClient(api_key=targon_key, org_slug=args.targon_org_slug)
    settings.lock_file.parent.mkdir(parents=True, exist_ok=True)
    with settings.lock_file.open("w") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            print("trusted image builder already running", file=sys.stderr)
            return 75
        while True:
            try:
                handled = run_one_submission(settings, client, submission_control)
                if not handled:
                    handled = run_one(settings, client, control)
            except ControllerError as error:
                # Claim/read outages are control-plane failures, not a reason to
                # terminate the daemon or mislabel a Targon rental. The next
                # bounded poll retries without exposing response bodies.
                print(str(error), file=sys.stderr)
                handled = False
            if args.once:
                return 0
            if not handled:
                time.sleep(settings.interval_seconds)


if __name__ == "__main__":
    raise SystemExit(main())
