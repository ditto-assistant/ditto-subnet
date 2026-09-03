#!/usr/bin/env python3
"""Run the managed-stack executor canary and write a diagnostic receipt."""

from __future__ import annotations

import argparse
import contextlib
import hashlib
import json
import os
import re
import stat
import subprocess
import sys
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit
from uuid import uuid4

ROOT = Path(__file__).resolve().parents[1]
COMPOSE_WRAPPER = ROOT / "scripts/validator-stack-compose.sh"
REVISION_RE = re.compile(r"^[0-9a-f]{40}$")
IMAGE_RE = re.compile(r"^[a-z0-9.-]+(?::[0-9]+)?/[a-z0-9._/-]+@sha256:[0-9a-f]{64}$")
DESCRIPTOR_RE = re.compile(
    r"^ghcr\.io/ditto-assistant/ditto-subnet-stack@sha256:[0-9a-f]{64}$"
)
HOTKEY_RE = re.compile(r"^[1-9A-HJ-NP-Za-km-z]{47,48}$")
EXPECTED_SECRET_SOURCES = {
    "coding-executor-validator-ca",
    "coding-executor-validator-client-cert",
    "coding-executor-validator-client-key",
}
EXPECTED_SECRET_TARGETS = {
    "coding-executor-validator-ca": "coding-executor-validator-ca.pem",
    "coding-executor-validator-client-cert": ("coding-executor-validator-client.pem"),
    "coding-executor-validator-client-key": (
        "coding-executor-validator-client-key.pem"
    ),
}
EXPECTED_RUNTIME_PATHS = {
    "VALIDATOR_CODING_EXECUTOR_CA_PATH": (
        "/run/secrets/coding-executor-validator-ca.pem"
    ),
    "VALIDATOR_CODING_EXECUTOR_CLIENT_CERT_PATH": (
        "/run/secrets/coding-executor-validator-client.pem"
    ),
    "VALIDATOR_CODING_EXECUTOR_CLIENT_KEY_PATH": (
        "/run/secrets/coding-executor-validator-client-key.pem"
    ),
}
MAX_COMPOSE_BYTES = 4 << 20


class CanaryRunnerError(RuntimeError):
    """A public, secret-free canary runner failure."""


@dataclass(frozen=True)
class CanaryPlan:
    source_revision: str
    validator_image: str
    descriptor_ref: str
    validator_hotkey: str
    executor_origin_sha256: str
    secret_paths: tuple[str, ...]


def validate_compose_model(model: object, expected_revision: str) -> CanaryPlan:
    if not REVISION_RE.fullmatch(expected_revision) or not isinstance(model, dict):
        raise CanaryRunnerError("canary compose provenance is invalid")
    services = model.get("services")
    secrets = model.get("secrets")
    if not isinstance(services, dict) or not isinstance(secrets, dict):
        raise CanaryRunnerError("canary compose model is incomplete")
    validator = services.get("ditto-subnet")
    if not isinstance(validator, dict) or "build" in validator:
        raise CanaryRunnerError("canary validator service is not immutable")
    image = validator.get("image")
    environment = validator.get("environment")
    service_secrets = validator.get("secrets")
    if (
        not isinstance(image, str)
        or not IMAGE_RE.fullmatch(image)
        or validator.get("pull_policy") != "never"
        or not isinstance(environment, dict)
        or not isinstance(service_secrets, list)
    ):
        raise CanaryRunnerError("canary validator release identity is invalid")
    required_environment = {
        "VALIDATOR_CODING_EXECUTOR_CONNECTIVITY_CANARY_ENABLED": "true",
        "VALIDATOR_CODING_EXECUTOR_REMOTE_ENABLED": "false",
        "VALIDATOR_CODING_SHADOW_ENABLED": "false",
        "VALIDATOR_STACK_MODE": "managed",
        "VALIDATOR_STACK_REVISION": expected_revision,
    }
    if any(
        environment.get(key) != value for key, value in required_environment.items()
    ):
        raise CanaryRunnerError("canary-only validator gates are invalid")
    if any(
        environment.get(key) != value for key, value in EXPECTED_RUNTIME_PATHS.items()
    ):
        raise CanaryRunnerError("canary credential paths are invalid")
    origin = environment.get("VALIDATOR_CODING_EXECUTOR_BASE_URL")
    hotkey = environment.get("VALIDATOR_HOTKEY")
    descriptor = environment.get("VALIDATOR_STACK_DESCRIPTOR_REF")
    timeout = environment.get("VALIDATOR_CODING_EXECUTOR_TIMEOUT_SECONDS")
    if (
        not isinstance(origin, str)
        or not _valid_private_origin(origin)
        or not isinstance(hotkey, str)
        or not HOTKEY_RE.fullmatch(hotkey)
        or not isinstance(descriptor, str)
        or not DESCRIPTOR_RE.fullmatch(descriptor)
        or not _valid_timeout(timeout)
    ):
        raise CanaryRunnerError("canary runtime authority is invalid")
    if len(service_secrets) != 3 or any(
        not isinstance(item, dict)
        or item.get("source") not in EXPECTED_SECRET_SOURCES
        or item.get("target") != EXPECTED_SECRET_TARGETS[item["source"]]
        or str(item.get("mode")) != "0400"
        for item in service_secrets
    ):
        raise CanaryRunnerError("canary secret mounts are invalid")
    sources = {item["source"] for item in service_secrets}
    if sources != EXPECTED_SECRET_SOURCES:
        raise CanaryRunnerError("canary secret mounts are invalid")
    secret_paths: list[str] = []
    for source in EXPECTED_SECRET_SOURCES:
        value = secrets.get(source)
        path = value.get("file") if isinstance(value, dict) else None
        if (
            not isinstance(path, str)
            or not os.path.isabs(path)
            or path == "/dev/null"
            or any(character in path for character in "\r\n")
        ):
            raise CanaryRunnerError("canary secret sources are invalid")
        secret_paths.append(path)
    if len(set(secret_paths)) != 3:
        raise CanaryRunnerError("canary secret sources are not distinct")
    return CanaryPlan(
        source_revision=expected_revision,
        validator_image=image,
        descriptor_ref=descriptor,
        validator_hotkey=hotkey,
        executor_origin_sha256=hashlib.sha256(origin.encode()).hexdigest(),
        secret_paths=tuple(sorted(secret_paths)),
    )


def build_receipt(
    plan: CanaryPlan, *, started_at: datetime, completed_at: datetime
) -> dict[str, object]:
    if (
        started_at.tzinfo is None
        or completed_at.tzinfo is None
        or completed_at < started_at
    ):
        raise CanaryRunnerError("canary receipt clock is invalid")
    return {
        "schema": "dittobench-coding-executor-connectivity-receipt-v1",
        "coding_contract_version": 1,
        "weight_eligible": False,
        "authority": "operator-local-diagnostic",
        "status": "passed",
        "source_revision": plan.source_revision,
        "validator_image": plan.validator_image,
        "descriptor_ref": plan.descriptor_ref,
        "validator_hotkey": plan.validator_hotkey,
        "executor_origin_sha256": plan.executor_origin_sha256,
        "ticket_authority_used": False,
        "platform_contacted": False,
        "candidate_executed": False,
        "s3_accessed": False,
        "started_at": started_at.astimezone(UTC).isoformat(timespec="seconds"),
        "completed_at": completed_at.astimezone(UTC).isoformat(timespec="seconds"),
    }


def write_receipt(path: Path, receipt: dict[str, object]) -> None:
    if not path.is_absolute() or path.name in {"", ".", ".."}:
        raise CanaryRunnerError("canary receipt path is invalid")
    parent = path.parent
    try:
        parent_metadata = parent.lstat()
    except OSError as error:
        raise CanaryRunnerError("canary receipt directory is unavailable") from error
    if (
        not stat.S_ISDIR(parent_metadata.st_mode)
        or stat.S_ISLNK(parent_metadata.st_mode)
        or parent_metadata.st_uid != os.geteuid()
        or stat.S_IMODE(parent_metadata.st_mode) != 0o700
    ):
        raise CanaryRunnerError("canary receipt directory is unsafe")
    if path.exists() or path.is_symlink():
        metadata = path.lstat()
        if (
            not stat.S_ISREG(metadata.st_mode)
            or stat.S_ISLNK(metadata.st_mode)
            or metadata.st_uid != os.geteuid()
            or stat.S_IMODE(metadata.st_mode) != 0o600
        ):
            raise CanaryRunnerError("existing canary receipt is unsafe")
    encoded = json.dumps(receipt, sort_keys=True, separators=(",", ":")).encode()
    encoded += b"\n"
    temporary = parent / f".{path.name}.{uuid4().hex}.tmp"
    descriptor = -1
    try:
        descriptor = os.open(
            temporary,
            os.O_CREAT | os.O_EXCL | os.O_WRONLY | os.O_NOFOLLOW,
            0o600,
        )
        remaining = memoryview(encoded)
        while remaining:
            written = os.write(descriptor, remaining)
            if written <= 0:
                raise OSError("short receipt write")
            remaining = remaining[written:]
        os.fsync(descriptor)
        os.close(descriptor)
        descriptor = -1
        os.replace(temporary, path)
        os.chmod(path, 0o600)
        directory = os.open(parent, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
    except OSError as error:
        raise CanaryRunnerError("canary receipt could not be committed") from error
    finally:
        if descriptor >= 0:
            os.close(descriptor)
        with contextlib.suppress(OSError):
            temporary.unlink(missing_ok=True)


def _valid_private_origin(value: str) -> bool:
    parsed = urlsplit(value)
    try:
        octets = tuple(int(part) for part in (parsed.hostname or "").split("."))
        port = parsed.port
    except ValueError:
        return False
    private = (
        len(octets) == 4
        and all(0 <= item <= 255 for item in octets)
        and (
            octets[0] == 10
            or octets[:2] == (192, 168)
            or (octets[0] == 172 and 16 <= octets[1] <= 31)
        )
    )
    return bool(
        private
        and parsed.scheme == "https"
        and port == 9443
        and parsed.username is None
        and parsed.password is None
        and parsed.path in {"", "/"}
        and not parsed.query
        and not parsed.fragment
    )


def _valid_timeout(value: object) -> bool:
    try:
        parsed = float(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return False
    return 1 <= parsed <= 300


def validate_secret_files(paths: tuple[str, ...]) -> None:
    if len(paths) != 3 or len(set(paths)) != 3:
        raise CanaryRunnerError("canary credential file set is invalid")
    for value in paths:
        path = Path(value)
        try:
            metadata = path.lstat()
        except OSError as error:
            raise CanaryRunnerError("canary credential file is unavailable") from error
        if (
            not path.is_absolute()
            or not stat.S_ISREG(metadata.st_mode)
            or stat.S_ISLNK(metadata.st_mode)
            or metadata.st_uid != os.geteuid()
            or stat.S_IMODE(metadata.st_mode) != 0o400
            or not 0 < metadata.st_size <= 1 << 20
        ):
            raise CanaryRunnerError("canary credential file is unsafe")


def _run(
    command: list[str],
    *,
    timeout: float,
    capture: bool,
    environment: dict[str, str] | None = None,
) -> subprocess.CompletedProcess[bytes]:
    try:
        return subprocess.run(
            command,
            cwd=ROOT,
            check=False,
            capture_output=capture,
            timeout=timeout,
            env=environment,
        )
    except (OSError, subprocess.TimeoutExpired) as error:
        raise CanaryRunnerError("canary subprocess failed") from error


def _git_revision() -> str:
    result = _run(["git", "rev-parse", "HEAD"], timeout=10, capture=True)
    value = result.stdout.decode(errors="replace").strip()
    if result.returncode != 0 or not REVISION_RE.fullmatch(value):
        raise CanaryRunnerError("canary source revision is unavailable")
    for command in (["git", "diff", "--quiet"], ["git", "diff", "--cached", "--quiet"]):
        if _run(command, timeout=10, capture=True).returncode != 0:
            raise CanaryRunnerError("canary source checkout is dirty")
    return value


def _arguments() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--release-dir", type=Path, required=True)
    parser.add_argument("--expected-revision", required=True)
    parser.add_argument("--evidence-out", type=Path, required=True)
    parser.add_argument("--executor-base-url", required=True)
    parser.add_argument("--timeout-seconds", type=float, default=120.0)
    parser.add_argument("--transport-timeout-seconds", type=float, default=30.0)
    return parser.parse_args()


def _canary_environment(base_url: str, timeout_seconds: float) -> dict[str, str]:
    if not _valid_private_origin(base_url) or not 10 <= timeout_seconds <= 300:
        raise CanaryRunnerError("canary environment authority is invalid")
    environment = os.environ.copy()
    environment.update(
        {
            "VALIDATOR_CODING_EXECUTOR_CONNECTIVITY_CANARY_ENABLED": "true",
            "VALIDATOR_CODING_EXECUTOR_REMOTE_ENABLED": "false",
            "VALIDATOR_CODING_SHADOW_ENABLED": "false",
            "VALIDATOR_CODING_EXECUTOR_BASE_URL": base_url,
            "VALIDATOR_CODING_EXECUTOR_CA_PATH": EXPECTED_RUNTIME_PATHS[
                "VALIDATOR_CODING_EXECUTOR_CA_PATH"
            ],
            "VALIDATOR_CODING_EXECUTOR_CLIENT_CERT_PATH": EXPECTED_RUNTIME_PATHS[
                "VALIDATOR_CODING_EXECUTOR_CLIENT_CERT_PATH"
            ],
            "VALIDATOR_CODING_EXECUTOR_CLIENT_KEY_PATH": EXPECTED_RUNTIME_PATHS[
                "VALIDATOR_CODING_EXECUTOR_CLIENT_KEY_PATH"
            ],
            "VALIDATOR_CODING_EXECUTOR_TIMEOUT_SECONDS": str(timeout_seconds),
        }
    )
    return environment


def main() -> int:
    args = _arguments()
    if (
        not args.release_dir.is_absolute()
        or not REVISION_RE.fullmatch(args.expected_revision)
        or not 10 <= args.timeout_seconds <= 300
        or _git_revision() != args.expected_revision
        or not COMPOSE_WRAPPER.is_file()
        or COMPOSE_WRAPPER.is_symlink()
    ):
        raise CanaryRunnerError("canary invocation authority is invalid")
    base = [str(COMPOSE_WRAPPER), str(args.release_dir)]
    environment = _canary_environment(
        args.executor_base_url,
        args.transport_timeout_seconds,
    )
    resolved = _run(
        base + ["config", "--format", "json"],
        timeout=60,
        capture=True,
        environment=environment,
    )
    if (
        resolved.returncode != 0
        or not resolved.stdout
        or len(resolved.stdout) > MAX_COMPOSE_BYTES
    ):
        raise CanaryRunnerError("canary compose model could not be resolved")
    try:
        model: Any = json.loads(resolved.stdout)
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise CanaryRunnerError("canary compose model is invalid") from error
    plan = validate_compose_model(model, args.expected_revision)
    validate_secret_files(plan.secret_paths)
    started_at = datetime.now(UTC)
    environment["DITTO_ALLOW_MANAGED_STACK_MUTATION"] = "true"
    try:
        execution = subprocess.run(
            base
            + [
                "run",
                "--rm",
                "--no-deps",
                "--pull",
                "never",
                "-T",
                "ditto-subnet",
            ],
            cwd=ROOT,
            check=False,
            timeout=args.timeout_seconds,
            env=environment,
        )
    except (OSError, subprocess.TimeoutExpired) as error:
        raise CanaryRunnerError("canary execution failed") from error
    if execution.returncode != 0:
        raise CanaryRunnerError("canary execution was rejected")
    receipt = build_receipt(
        plan,
        started_at=started_at,
        completed_at=datetime.now(UTC),
    )
    write_receipt(args.evidence_out, receipt)
    print("coding executor connectivity canary passed; diagnostic receipt committed")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except CanaryRunnerError as error:
        print(f"error: {error}", file=sys.stderr)
        raise SystemExit(1) from None
