"""Persistent screener node with disposable KVM build and smoke guests.

The enrolled node credential never enters a guest. Platform mints an
attempt-bound build capability; the host places only that capability in a
mode-0600 cloud-init seed, destroys the VM, and removes its overlay after the
job reaches a terminal Platform state. Runtime smoke gets no Platform secret at
all: the guest emits one bounded marker and the trusted host reports the result.
"""

from __future__ import annotations

import argparse
import base64
import concurrent.futures
import contextlib
import json
import os
import re
import shlex
import shutil
import signal
import socket
import subprocess
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal

from screener_capacity.controller import ControllerError, _read_secret_file

_TERMINAL = {"succeeded", "fallback_required", "canceled", "consumed"}
_JOB_ID = re.compile(r"^[0-9a-f-]{36}$")
_IMAGE = re.compile(r"^[a-z0-9.-]+(?::[0-9]+)?/[a-z0-9._/-]+@sha256:[0-9a-f]{64}$")
_RUNTIME_MARKER = "DITTO_FLEET_RUNTIME_OK"
_GUEST_OSINFO = "debian12"
_LIBVIRT_URI = "qemu:///system"
_SERIAL_CAPTURE_LIMIT = 64_000


class _SerialCapture:
    def __init__(self, path: Path) -> None:
        self.path = path
        self._ready = threading.Event()
        self._stop = threading.Event()
        self._done = threading.Event()
        self._buffer = bytearray()
        self._thread = threading.Thread(target=self._run, daemon=True)

    def start(self) -> None:
        self._thread.start()
        if not self._ready.wait(5):
            raise OSError("serial capture socket did not start")

    def _run(self) -> None:
        with contextlib.suppress(FileNotFoundError):
            self.path.unlink()
        try:
            with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as server:
                server.bind(str(self.path))
                os.chmod(self.path, 0o600)
                server.listen(1)
                server.settimeout(0.25)
                self._ready.set()
                while not self._stop.is_set():
                    try:
                        connection, _ = server.accept()
                        break
                    except TimeoutError:
                        continue
                else:
                    return
                with connection:
                    connection.settimeout(0.25)
                    while not self._stop.is_set():
                        try:
                            chunk = connection.recv(4096)
                        except TimeoutError:
                            continue
                        if not chunk:
                            break
                        self._buffer.extend(chunk)
                        if len(self._buffer) > _SERIAL_CAPTURE_LIMIT:
                            del self._buffer[:-_SERIAL_CAPTURE_LIMIT]
        finally:
            self._ready.set()
            self._done.set()

    def finish(self) -> str:
        self._done.wait(2)
        self._stop.set()
        self._thread.join(2)
        with contextlib.suppress(FileNotFoundError):
            self.path.unlink()
        return bytes(self._buffer).decode(errors="replace")


@dataclass(frozen=True)
class NodeCredential:
    environment: str
    node_id: str
    screener_hotkey: str
    api_token: str


@dataclass(frozen=True)
class ChannelSettings:
    revision: int
    screening_concurrency: int
    sandbox_slots: int
    build_concurrency: int
    runtime_concurrency: int
    source_review_concurrency: int


@dataclass(frozen=True)
class Settings:
    platform_url: str
    credential_file: Path
    base_image: Path
    builder_image: str
    source_review_api_key_file: Path
    jobs_root: Path
    source_review_env_file: Path | None
    interval_seconds: float
    build_timeout_seconds: int
    runtime_timeout_seconds: int
    source_review_timeout_seconds: int
    max_workers: int
    vm_memory_mib: int
    vm_vcpus: int
    vm_disk_gib: int
    once: bool


def _load_credential(path: Path) -> NodeCredential:
    try:
        value = json.loads(path.read_text())
        credential = NodeCredential(
            environment=str(value["environment"]),
            node_id=str(value["node_id"]),
            screener_hotkey=str(value["screener_hotkey"]),
            api_token=str(value["api_token"]),
        )
    except (OSError, KeyError, TypeError, ValueError, json.JSONDecodeError) as error:
        raise ControllerError("screener node credential is unavailable") from error
    if (
        not credential.environment
        or not credential.node_id
        or len(credential.api_token) < 43
    ):
        raise ControllerError("screener node credential is invalid")
    return credential


class NodeControl:
    def __init__(self, *, platform_url: str, credential_file: Path) -> None:
        self.base = platform_url.rstrip("/")
        self.credential_file = credential_file

    def _request(
        self,
        method: str,
        path: str,
        *,
        payload: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        credential = _load_credential(self.credential_file)
        request = urllib.request.Request(
            f"{self.base}/api/v1/screener{path}",
            data=(
                json.dumps(payload, separators=(",", ":")).encode()
                if payload is not None
                else None
            ),
            headers={
                "Accept": "application/json",
                "Authorization": f"Bearer {credential.api_token}",
                "X-Screener-Hotkey": credential.screener_hotkey,
                **({"Content-Type": "application/json"} if payload is not None else {}),
            },
            method=method,
        )
        try:
            with urllib.request.urlopen(request, timeout=30) as response:
                body = response.read()
        except urllib.error.HTTPError as error:
            raise ControllerError(
                f"Platform node {method} failed with HTTP {error.code}"
            ) from None
        except (TimeoutError, urllib.error.URLError, OSError) as error:
            raise ControllerError("Platform node transport failed") from error
        try:
            value = json.loads(body) if body else {}
        except json.JSONDecodeError as error:
            raise ControllerError("Platform node response is invalid") from error
        if not isinstance(value, dict):
            raise ControllerError("Platform node response has invalid shape")
        return value

    def settings(self) -> ChannelSettings:
        body = self._request("GET", "/nodes/channel-settings")
        values = body.get("settings")
        if not isinstance(values, dict):
            raise ControllerError("Platform node settings are invalid")
        try:
            settings = ChannelSettings(
                revision=int(body["revision"]),
                screening_concurrency=int(values["screening_concurrency"]),
                sandbox_slots=int(values["sandbox_slots"]),
                build_concurrency=int(values["build_concurrency"]),
                runtime_concurrency=int(values["runtime_concurrency"]),
                source_review_concurrency=int(values["source_review_concurrency"]),
            )
        except (KeyError, TypeError, ValueError) as error:
            raise ControllerError("Platform node settings are invalid") from error
        if (
            min(
                settings.revision,
                settings.screening_concurrency,
                settings.sandbox_slots,
                settings.build_concurrency,
                settings.runtime_concurrency,
                settings.source_review_concurrency,
            )
            < 0
        ):
            raise ControllerError("Platform node settings are invalid")
        return settings

    def claim(
        self, lane: Literal["build", "runtime", "source_review"]
    ) -> dict[str, Any] | None:
        path = {
            "build": "/nodes/jobs/submission-image-builds/claim",
            "runtime": "/nodes/jobs/submission-runtime-smokes/claim",
            "source_review": "/nodes/jobs/submission-source-reviews/claim",
        }[lane]
        key = {"build": "build", "runtime": "artifact", "source_review": "review"}[lane]
        environment = _load_credential(self.credential_file).environment
        value = self._request("POST", path, payload={"environment": environment}).get(
            key
        )
        if value is None:
            return None
        if not isinstance(value, dict):
            raise ControllerError("Platform node claim is invalid")
        return value

    def update(
        self,
        lane: Literal["build", "source_review"],
        job_id: str,
        *,
        status: Literal["running", "fallback_required"],
        resource_id: str | None = None,
        error_code: str | None = None,
    ) -> None:
        noun = (
            "submission-image-builds"
            if lane == "build"
            else "submission-source-reviews"
        )
        self._request(
            "PUT",
            f"/nodes/jobs/{noun}/{job_id}",
            payload={
                "status": status,
                "provider_resource_id": resource_id,
                "error_code": error_code,
            },
        )

    def status(self, lane: Literal["build", "source_review"], job_id: str) -> str:
        noun = (
            "submission-image-builds"
            if lane == "build"
            else "submission-source-reviews"
        )
        value = self._request("GET", f"/nodes/jobs/{noun}/{job_id}").get("status")
        if not isinstance(value, str):
            raise ControllerError("Platform node job status is invalid")
        return value

    def runtime_result(
        self,
        build_id: str,
        *,
        status: Literal["running", "succeeded", "fallback_required"],
        resource_id: str,
        error_code: str | None = None,
    ) -> None:
        self._request(
            "POST",
            f"/nodes/jobs/submission-runtime-smokes/{build_id}/result",
            payload={
                "status": status,
                "provider_resource_id": resource_id,
                "error_code": error_code,
            },
        )


def _cloud_config(script: str) -> str:
    encoded = base64.b64encode(script.encode()).decode()
    return "\n".join(
        (
            "#cloud-config",
            "write_files:",
            "  - path: /usr/local/sbin/ditto-fleet-job",
            "    owner: root:root",
            "    permissions: '0700'",
            "    encoding: b64",
            f"    content: {encoded}",
            "runcmd:",
            "  - [ /usr/local/sbin/ditto-fleet-job ]",
            "power_state:",
            "  mode: poweroff",
            "  timeout: 30",
            "  condition: true",
            "",
        )
    )


def _build_script(*, platform_url: str, build_id: str, token: str, image: str) -> str:
    if (
        _JOB_ID.fullmatch(build_id) is None
        or _IMAGE.fullmatch(image) is None
        or not platform_url.startswith("https://")
        or len(token) < 43
    ):
        raise ControllerError("build claim contains an invalid identifier")
    token_b64 = base64.b64encode(token.encode()).decode()
    return f"""#!/bin/sh
set -eu
umask 077
mkdir -p /run/ditto-job
printf %s {token_b64} | base64 -d >/run/ditto-job/token
DITTO_BUILD_JOB_TOKEN=$(cat /run/ditto-job/token)
export DITTO_BUILD_JOB_TOKEN
docker run --rm --network host \\
  -e DITTO_PLATFORM_URL={shlex.quote(platform_url)} \\
  -e DITTO_BUILD_ID={build_id} \\
  -e DITTO_BUILD_JOB_TOKEN \\
  -e DITTO_BUILD_EXIT_AFTER_COMPLETE=1 \\
  {image}
"""


def _runtime_script(*, archive_url_b64: str, expected_sha256: str) -> str:
    try:
        archive_url = base64.b64decode(archive_url_b64, validate=True).decode()
    except (ValueError, UnicodeError) as error:
        raise ControllerError("runtime claim contains an invalid URL") from error
    if re.fullmatch(
        r"[0-9a-f]{64}", expected_sha256
    ) is None or not archive_url.startswith("https://"):
        raise ControllerError("runtime claim contains an invalid digest")
    return f"""#!/bin/sh
set -eu
umask 077
mkdir -p /run/ditto-job
printf %s {archive_url_b64} | base64 -d >/run/ditto-job/url
curl --fail --silent --show-error --location \\
  --output /run/ditto-job/image.tar "$(cat /run/ditto-job/url)"
actual=$(sha256sum /run/ditto-job/image.tar | cut -d' ' -f1)
test "$actual" = {expected_sha256}
loaded=$(docker load --input /run/ditto-job/image.tar)
image=$(printf '%s\n' "$loaded" | sed -n 's/^Loaded image: //p' | tail -n1)
test -n "$image"
container=ditto-runtime-smoke
docker run --detach --name "$container" --read-only --user 65532:65532 \\
  --cap-drop ALL --security-opt no-new-privileges --pids-limit 256 \\
  --memory 4g --tmpfs /tmp:rw,nosuid,nodev,noexec,size=64m \\
  --env OPENROUTER_API_KEY=sk-screener-smoke \\
  --env DITTOBENCH_DB=/tmp/dittobench.db \\
  --publish 127.0.0.1::8080 "$image" >/dev/null
port=$(docker port "$container" 8080/tcp | sed 's/.*://')
i=0
while [ "$i" -lt 60 ]; do
  if curl --fail --silent --max-time 2 "http://127.0.0.1:$port/health" >/dev/null; then
    printf '{_RUNTIME_MARKER}\n' >/dev/ttyS0
    exit 0
  fi
  i=$((i + 1))
  sleep 1
done
exit 1
"""


class KVMRunner:
    def __init__(
        self,
        *,
        base_image: Path,
        jobs_root: Path,
        memory_mib: int,
        vcpus: int,
        disk_gib: int,
    ) -> None:
        self.base_image = base_image
        self.jobs_root = jobs_root
        self.memory_mib = memory_mib
        self.vcpus = vcpus
        self.disk_gib = disk_gib

    def run(self, *, name: str, script: str, timeout_seconds: int) -> tuple[bool, str]:
        job_dir = Path(tempfile.mkdtemp(prefix=f"{name}-", dir=self.jobs_root))
        # qemu:///system runs the VM as libvirt-qemu. Ansible adds that user to
        # the directory's group; grant only the minimum group access needed for
        # the overlay, seed, and serial console.
        os.chmod(job_dir, 0o770)
        overlay = job_dir / "overlay.qcow2"
        user_data = job_dir / "user-data"
        meta_data = job_dir / "meta-data"
        seed = job_dir / "seed.iso"
        domain = re.sub(r"[^a-zA-Z0-9_-]", "-", name)[:63]
        console_socket = self.jobs_root / f".{domain[:40]}.sock"
        serial = _SerialCapture(console_socket)
        try:
            subprocess.run(
                [
                    "qemu-img",
                    "create",
                    "-q",
                    "-f",
                    "qcow2",
                    "-F",
                    "qcow2",
                    "-b",
                    str(self.base_image),
                    str(overlay),
                ],
                check=True,
                timeout=30,
            )
            subprocess.run(
                ["qemu-img", "resize", "-q", str(overlay), f"{self.disk_gib}G"],
                check=True,
                timeout=30,
            )
            os.chmod(overlay, 0o640)
            user_data.write_text(_cloud_config(script))
            meta_data.write_text(f"instance-id: {domain}\nlocal-hostname: {domain}\n")
            os.chmod(user_data, 0o600)
            os.chmod(meta_data, 0o600)
            subprocess.run(
                ["cloud-localds", str(seed), str(user_data), str(meta_data)],
                check=True,
                timeout=30,
            )
            os.chmod(seed, 0o640)
            serial.start()
            subprocess.run(
                [
                    "virt-install",
                    "--connect",
                    _LIBVIRT_URI,
                    "--name",
                    domain,
                    "--memory",
                    str(self.memory_mib),
                    "--vcpus",
                    str(self.vcpus),
                    "--cpu",
                    "host-passthrough",
                    "--osinfo",
                    _GUEST_OSINFO,
                    "--import",
                    "--transient",
                    "--noautoconsole",
                    "--graphics",
                    "none",
                    "--disk",
                    f"path={overlay},format=qcow2,bus=virtio,cache=none",
                    "--disk",
                    f"path={seed},format=raw,bus=virtio,readonly=on",
                    "--network",
                    "network=ditto-screener-nat,model=virtio",
                    "--serial",
                    f"unix,path={console_socket},mode=connect",
                ],
                check=True,
                timeout=60,
            )
            deadline = time.monotonic() + timeout_seconds
            while time.monotonic() < deadline:
                state = subprocess.run(
                    ["virsh", "--connect", _LIBVIRT_URI, "domstate", domain],
                    capture_output=True,
                    text=True,
                    timeout=15,
                    check=False,
                )
                if state.returncode != 0 or "shut off" in state.stdout.casefold():
                    break
                time.sleep(2)
            else:
                return False, serial.finish() or "timeout"
            return True, serial.finish()
        except (OSError, subprocess.SubprocessError) as error:
            return False, serial.finish() or type(error).__name__
        finally:
            with contextlib.suppress(OSError, subprocess.SubprocessError):
                subprocess.run(
                    ["virsh", "--connect", _LIBVIRT_URI, "destroy", domain],
                    capture_output=True,
                    timeout=15,
                    check=False,
                )
            serial.finish()
            shutil.rmtree(job_dir, ignore_errors=True)


class FleetNode:
    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self.control = NodeControl(
            platform_url=settings.platform_url,
            credential_file=settings.credential_file,
        )
        self.runner = KVMRunner(
            base_image=settings.base_image,
            jobs_root=settings.jobs_root,
            memory_mib=settings.vm_memory_mib,
            vcpus=settings.vm_vcpus,
            disk_gib=settings.vm_disk_gib,
        )
        self.executor = concurrent.futures.ThreadPoolExecutor(
            max_workers=settings.max_workers, thread_name_prefix="ditto-fleet"
        )
        self.futures: dict[concurrent.futures.Future[None], str] = {}
        self.stop_requested = threading.Event()

    def request_stop(self) -> None:
        """Stop claiming new work; active futures drain before process exit."""
        self.stop_requested.set()

    def _counts(self) -> dict[str, int]:
        counts = {"build": 0, "runtime": 0, "source_review": 0}
        for lane in self.futures.values():
            counts[lane] += 1
        return counts

    def _reap(self) -> None:
        for future in tuple(self.futures):
            if not future.done():
                continue
            lane = self.futures.pop(future)
            try:
                future.result()
            except Exception as error:  # noqa: BLE001 - keep other lanes alive
                print(
                    f"fleet {lane} job failed: {type(error).__name__}",
                    file=sys.stderr,
                )

    def _run_build(self, build: dict[str, Any]) -> None:
        build_id = str(build["build_id"])
        domain = f"ditto-build-{build_id.replace('-', '')[:16]}"
        self.control.update("build", build_id, status="running", resource_id=domain)
        ok, _console = self.runner.run(
            name=domain,
            script=_build_script(
                platform_url=self.settings.platform_url,
                build_id=build_id,
                token=str(build["job_token"]),
                image=self.settings.builder_image,
            ),
            timeout_seconds=self.settings.build_timeout_seconds,
        )
        status = self.control.status("build", build_id)
        if status not in _TERMINAL:
            self.control.update(
                "build",
                build_id,
                status="fallback_required",
                resource_id=domain,
                error_code=(
                    "FLEET_SUBMISSION_BUILD_VM_EXITED"
                    if ok
                    else "FLEET_SUBMISSION_BUILD_VM_FAILED"
                ),
            )

    def _run_runtime(self, artifact: dict[str, Any]) -> None:
        build_id = str(artifact["build_id"])
        domain = f"ditto-smoke-{build_id.replace('-', '')[:16]}"
        self.control.runtime_result(build_id, status="running", resource_id=domain)
        ok, console = self.runner.run(
            name=domain,
            script=_runtime_script(
                archive_url_b64=str(artifact["archive_url_b64"]),
                expected_sha256=str(artifact["output_sha256"]),
            ),
            timeout_seconds=self.settings.runtime_timeout_seconds,
        )
        passed = ok and _RUNTIME_MARKER in console
        self.control.runtime_result(
            build_id,
            status="succeeded" if passed else "fallback_required",
            resource_id=domain,
            error_code=None if passed else "FLEET_RUNTIME_HEALTH_FAILED",
        )

    def _run_source_review(self, review: dict[str, Any]) -> None:
        review_id = str(review["review_id"])
        name = f"ditto-source-{review_id.replace('-', '')[:16]}"
        image = str(review["image_reference"])
        if _IMAGE.fullmatch(image) is None:
            raise ControllerError("source-review claim contains an invalid image")
        self.control.update(
            "source_review", review_id, status="running", resource_id=name
        )
        environment = dict(os.environ)
        environment.update(
            {
                "DITTO_PLATFORM_URL": self.settings.platform_url,
                "DITTO_SOURCE_REVIEW_ID": review_id,
                "DITTO_SOURCE_REVIEW_ATTEMPT_ID": str(review["attempt_id"]),
                "DITTO_SOURCE_REVIEW_ARTIFACT_SHA256": str(review["artifact_sha256"]),
                "DITTO_SOURCE_REVIEW_JOB_TOKEN": str(review["job_token"]),
                "DITTO_SOURCE_REVIEW_JOB": "1",
                "SCREENER_SOURCE_REVIEW_API_KEY_FILE": (
                    "/run/secrets/source-review-api-key"
                ),
                "SCREENER_STATIC_PREFLIGHT_V2_MODE": "off",
                "SCREENER_SOURCE_REVIEW_TIMEOUT_SECONDS": str(
                    self.settings.source_review_timeout_seconds
                ),
            }
        )
        command = [
            "docker",
            "run",
            "--rm",
            "--name",
            name,
            "--read-only",
            "--cap-drop",
            "ALL",
            "--security-opt",
            "no-new-privileges",
            "--pids-limit",
            "512",
            "--memory",
            "4g",
            "--tmpfs",
            "/tmp:rw,nosuid,nodev,noexec,size=2g",
            "--volume",
            f"{self.settings.source_review_api_key_file}:/run/secrets/source-review-api-key:ro",
            "--env",
            "DITTO_PLATFORM_URL",
            "--env",
            "DITTO_SOURCE_REVIEW_ID",
            "--env",
            "DITTO_SOURCE_REVIEW_ATTEMPT_ID",
            "--env",
            "DITTO_SOURCE_REVIEW_ARTIFACT_SHA256",
            "--env",
            "DITTO_SOURCE_REVIEW_JOB_TOKEN",
            "--env",
            "DITTO_SOURCE_REVIEW_JOB",
            "--env",
            "SCREENER_SOURCE_REVIEW_API_KEY_FILE",
            "--env",
            "SCREENER_SOURCE_REVIEW_TIMEOUT_SECONDS",
            "--env",
            "SCREENER_STATIC_PREFLIGHT_V2_MODE",
        ]
        if self.settings.source_review_env_file is not None:
            command.extend(["--env-file", str(self.settings.source_review_env_file)])
        command.extend([image, "python", "-m", "ditto_screener.source_review_job"])
        result = subprocess.run(
            command,
            env=environment,
            capture_output=True,
            timeout=self.settings.source_review_timeout_seconds + 120,
            check=False,
        )
        status = self.control.status("source_review", review_id)
        if status not in _TERMINAL:
            self.control.update(
                "source_review",
                review_id,
                status="fallback_required",
                resource_id=name,
                error_code=(
                    "FLEET_SOURCE_REVIEW_FAILED"
                    if result.returncode != 0
                    else "FLEET_SOURCE_REVIEW_INCOMPLETE"
                ),
            )

    def tick(self) -> bool:
        self._reap()
        limits = self.control.settings()
        counts = self._counts()
        handled = False
        sandbox_active = counts["build"] + counts["runtime"]

        # Finish already-built work before admitting another expensive build.
        while (
            counts["runtime"] < limits.runtime_concurrency
            and sandbox_active < limits.sandbox_slots
        ):
            artifact = self.control.claim("runtime")
            if artifact is None:
                break
            future = self.executor.submit(self._run_runtime, artifact)
            self.futures[future] = "runtime"
            counts["runtime"] += 1
            sandbox_active += 1
            handled = True

        while (
            counts["build"] < limits.build_concurrency
            and sandbox_active < limits.sandbox_slots
        ):
            build = self.control.claim("build")
            if build is None:
                break
            future = self.executor.submit(self._run_build, build)
            self.futures[future] = "build"
            counts["build"] += 1
            sandbox_active += 1
            handled = True

        while counts["source_review"] < limits.source_review_concurrency:
            review = self.control.claim("source_review")
            if review is None:
                break
            future = self.executor.submit(self._run_source_review, review)
            self.futures[future] = "source_review"
            counts["source_review"] += 1
            handled = True
        return handled

    def run(self) -> int:
        try:
            while not self.stop_requested.is_set():
                handled = self.tick()
                if self.settings.once:
                    return 0
                if not handled:
                    self.stop_requested.wait(self.settings.interval_seconds)
        finally:
            self.executor.shutdown(wait=True, cancel_futures=False)
        return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser()
    parser.add_argument("--platform-url", required=True)
    parser.add_argument("--credential-file", type=Path, required=True)
    parser.add_argument("--base-image", type=Path, required=True)
    parser.add_argument("--builder-image", required=True)
    parser.add_argument("--source-review-api-key-file", type=Path, required=True)
    parser.add_argument("--source-review-env-file", type=Path)
    parser.add_argument("--jobs-root", type=Path, required=True)
    parser.add_argument("--interval-seconds", type=float, default=2.0)
    parser.add_argument("--build-timeout-seconds", type=int, default=3000)
    parser.add_argument("--runtime-timeout-seconds", type=int, default=300)
    parser.add_argument("--source-review-timeout-seconds", type=int, default=3600)
    parser.add_argument("--max-workers", type=int, default=16)
    parser.add_argument("--vm-memory-mib", type=int, default=10240)
    parser.add_argument("--vm-vcpus", type=int, default=8)
    parser.add_argument("--vm-disk-gib", type=int, default=80)
    parser.add_argument("--once", action="store_true")
    return parser


def main() -> int:
    args = build_parser().parse_args()
    if not args.platform_url.startswith("https://"):
        raise ControllerError("Platform URL must use HTTPS")
    if _IMAGE.fullmatch(args.builder_image) is None:
        raise ControllerError("builder image must be pinned by digest")
    if not args.base_image.is_file():
        raise ControllerError("fleet base image is unavailable")
    _load_credential(args.credential_file)
    _read_secret_file(args.source_review_api_key_file)
    args.jobs_root.mkdir(parents=True, exist_ok=True, mode=0o770)
    os.chmod(args.jobs_root, 0o770)
    settings = Settings(
        platform_url=args.platform_url,
        credential_file=args.credential_file,
        base_image=args.base_image,
        builder_image=args.builder_image,
        source_review_api_key_file=args.source_review_api_key_file,
        jobs_root=args.jobs_root,
        source_review_env_file=args.source_review_env_file,
        interval_seconds=max(0.5, args.interval_seconds),
        build_timeout_seconds=max(60, args.build_timeout_seconds),
        runtime_timeout_seconds=max(60, args.runtime_timeout_seconds),
        source_review_timeout_seconds=max(60, args.source_review_timeout_seconds),
        max_workers=max(1, args.max_workers),
        vm_memory_mib=min(12288, max(8192, args.vm_memory_mib)),
        vm_vcpus=min(16, max(2, args.vm_vcpus)),
        vm_disk_gib=min(160, max(32, args.vm_disk_gib)),
        once=args.once,
    )
    node = FleetNode(settings)
    for handled_signal in (signal.SIGTERM, signal.SIGINT):
        signal.signal(handled_signal, lambda _signum, _frame: node.request_stop())
    return node.run()


if __name__ == "__main__":
    raise SystemExit(main())
