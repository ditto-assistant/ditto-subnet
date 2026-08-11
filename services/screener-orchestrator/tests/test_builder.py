import base64
import json
from pathlib import Path
from typing import Any

from screener_capacity.builder import (
    Settings,
    _docker_config,
    _kaniko_script,
    run_one_submission,
)
from screener_capacity.targon import TargonAPIError


def test_registry_config_uses_short_lived_oauth_token() -> None:
    encoded = _docker_config(
        "us-central1-docker.pkg.dev/p/r/screener:sha-a", "short-lived-token"
    )
    value = json.loads(base64.b64decode(encoded))
    assert (
        value["auths"]["us-central1-docker.pkg.dev"]["username"] == "oauth2accesstoken"
    )
    assert (
        value["auths"]["us-central1-docker.pkg.dev"]["password"] == "short-lived-token"
    )


def test_kaniko_job_is_bound_to_exact_monorepo_sha_and_paths() -> None:
    sha = "a" * 40
    build = {
        "source_repository": "https://github.com/ditto-assistant/ditto-subnet.git",
        "source_sha": sha,
        "dockerfile_path": "workers/screener/Dockerfile",
        "destination": "us-central1-docker.pkg.dev/p/r/screener:sha-a",
    }
    script = _kaniko_script(build)
    assert f"/archive/{sha}.tar.gz" in script
    assert f"--context=/workspace/src/ditto-subnet-{sha}" in script
    assert "--dockerfile=workers/screener/Dockerfile" in script
    assert "DITTO_BUILD_DIGEST=" in script
    assert "short-lived-token" not in script


class _SubmissionControl:
    base = "https://platform.example"

    def __init__(self, build: dict[str, Any] | None) -> None:
        self.build = build
        self.updates: list[tuple[str, dict[str, Any]]] = []
        self.cleanup: list[tuple[str, str]] = []

    def claim(self) -> dict[str, Any] | None:
        return self.build

    def update(self, build_id: str, **values: Any) -> None:
        self.updates.append((build_id, values))

    def cleanup_required(self, build_id: str, *, provider_resource_id: str) -> None:
        self.cleanup.append((build_id, provider_resource_id))

    def status(self, _build_id: str) -> str:
        return "succeeded"


class _Targon:
    def __init__(self, *, delete_fails: bool = False, available: int = 1) -> None:
        self.delete_fails = delete_fails
        self.available = available
        self.created: dict[str, Any] | None = None
        self.deployed: list[str] = []
        self.suspended: list[str] = []
        self.deleted: list[str] = []

    def inventory(self) -> list[dict[str, Any]]:
        return [{"name": "cpu-small", "available": self.available}]

    def create_rental(self, **values: Any) -> dict[str, Any]:
        self.created = values
        return {"uid": "wrk-build-1"}

    def deploy(self, uid: str) -> dict[str, Any]:
        self.deployed.append(uid)
        return {}

    def state(self, _uid: str) -> dict[str, Any]:
        return {"status": "running"}

    def suspend(self, uid: str) -> dict[str, Any]:
        self.suspended.append(uid)
        return {}

    def delete(self, uid: str) -> None:
        self.deleted.append(uid)
        if self.delete_fails:
            raise TargonAPIError(operation="DELETE", status=500, reason="HTTP error")


def _settings() -> Settings:
    return Settings(
        environment="prod",
        epoch="builder:test",
        targon_resource="cpu-small",
        kaniko_image="kaniko@example",
        registry_service_account="builder@example.test",
        provision_timeout_seconds=1,
        build_timeout_seconds=1,
        interval_seconds=1,
        lock_file=Path("/tmp/test-builder.lock"),
        submission_builder_image="public.example/submission-builder@sha256:" + "a" * 64,
    )


def _submission() -> dict[str, Any]:
    return {
        "build_id": "550e8400-e29b-41d4-a716-446655440000",
        "job_token": "job-capability-" + "x" * 48,
    }


def test_submission_rental_receives_only_attempt_capability_and_pinned_image() -> None:
    control = _SubmissionControl(_submission())
    targon = _Targon()

    assert run_one_submission(_settings(), targon, control)

    assert targon.created == {
        "name": "ditto-miner-build-550e8400e29b",
        "image": _settings().submission_builder_image,
        "resource_name": "cpu-small",
        "envs": [
            {"name": "DITTO_PLATFORM_URL", "value": control.base},
            {"name": "DITTO_BUILD_ID", "value": _submission()["build_id"]},
            {
                "name": "DITTO_BUILD_JOB_TOKEN",
                "value": _submission()["job_token"],
            },
        ],
    }
    assert targon.deployed == ["wrk-build-1"]
    assert targon.suspended == ["wrk-build-1"]
    assert targon.deleted == ["wrk-build-1"]
    assert control.updates == [
        (
            _submission()["build_id"],
            {"status": "running", "provider_resource_id": "wrk-build-1"},
        )
    ]


def test_submission_capacity_miss_authorizes_local_fallback_without_rental() -> None:
    control = _SubmissionControl(_submission())
    targon = _Targon(available=0)

    assert run_one_submission(_settings(), targon, control)

    assert targon.created is None
    assert control.updates == [
        (
            _submission()["build_id"],
            {
                "status": "fallback_required",
                "error_code": "TARGON_SUBMISSION_BUILD_CAPACITY_UNAVAILABLE",
            },
        )
    ]


def test_submission_delete_failure_is_suspended_and_audited() -> None:
    control = _SubmissionControl(_submission())
    targon = _Targon(delete_fails=True)

    assert run_one_submission(_settings(), targon, control)

    assert targon.suspended == ["wrk-build-1"]
    assert control.cleanup == [(_submission()["build_id"], "wrk-build-1")]
