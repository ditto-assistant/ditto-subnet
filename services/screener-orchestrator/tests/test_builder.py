import base64
import json
from pathlib import Path
from typing import Any

from screener_capacity.builder import (
    Settings,
    SubmissionBuildControl,
    _delete_rental,
    _docker_config,
    _kaniko_script,
    run_one_runtime_smoke,
    run_one_source_review,
    run_one_submission,
)
from screener_capacity.controller import ControllerError
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

    def __init__(
        self, build: dict[str, Any] | None, *, status: str = "succeeded"
    ) -> None:
        self.build = build
        self.build_status = status
        self.updates: list[tuple[str, dict[str, Any]]] = []
        self.cleanup: list[tuple[str, str]] = []

    def claim(self) -> dict[str, Any] | None:
        return self.build

    def update(self, build_id: str, **values: Any) -> None:
        self.updates.append((build_id, values))

    def cleanup_required(self, build_id: str, *, provider_resource_id: str) -> None:
        self.cleanup.append((build_id, provider_resource_id))

    def status(self, _build_id: str) -> str:
        return self.build_status


class _Targon:
    def __init__(
        self,
        *,
        delete_fails: bool = False,
        available: int = 1,
        deploy_error: TargonAPIError | None = None,
        state_status: str = "running",
        state_message: str = "",
        state_after_delete_failure: str | None = None,
    ) -> None:
        self.delete_fails = delete_fails
        self.available = available
        self.deploy_error = deploy_error
        self.state_status = state_status
        self.state_message = state_message
        self.state_after_delete_failure = state_after_delete_failure
        self.created: dict[str, Any] | None = None
        self.deployed: list[str] = []
        self.suspended: list[str] = []
        self.deleted: list[str] = []
        self.updated: list[tuple[str, dict[str, Any]]] = []

    def inventory(self) -> list[dict[str, Any]]:
        return [{"name": "cpu-small", "available": self.available}]

    def create_rental(self, **values: Any) -> dict[str, Any]:
        self.created = values
        return {"uid": "wrk-build-1"}

    def deploy(self, uid: str) -> dict[str, Any]:
        self.deployed.append(uid)
        if self.deploy_error is not None:
            raise self.deploy_error
        return {}

    def update(self, uid: str, **values: Any) -> dict[str, Any]:
        self.updated.append((uid, values))
        return {}

    def state(self, _uid: str) -> dict[str, Any]:
        status = self.state_status
        if self.deleted and self.delete_fails and self.state_after_delete_failure:
            status = self.state_after_delete_failure
        return {
            "status": status,
            "message": self.state_message,
            "urls": [{"port": 8080, "url": "https://runtime.example"}],
        }

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
        gcp_bootstrap_service_account="bootstrap@example.test",
        gcp_bootstrap_delegate_service_account=None,
        source_review_secret_resource="projects/test/secrets/reviewer",
        source_review_timeout_seconds=1,
        candidate_registry_service_account="candidate@example.test",
        runtime_timeout_seconds=1,
    )


def _submission() -> dict[str, Any]:
    return {
        "build_id": "550e8400-e29b-41d4-a716-446655440000",
        "job_token": "job-capability-" + "x" * 48,
    }


class _SourceControl(_SubmissionControl):
    def status(self, _review_id: str) -> str:
        return self.build_status


def _source_review() -> dict[str, Any]:
    return {
        "review_id": "550e8400-e29b-41d4-a716-446655440000",
        "attempt_id": "650e8400-e29b-41d4-a716-446655440000",
        "artifact_sha256": "b" * 64,
        "image_reference": "public.example/screener@sha256:" + "a" * 64,
        "job_token": "job-capability-" + "x" * 48,
    }


def test_source_review_runs_as_one_shot_pinned_rental(monkeypatch) -> None:
    sleeps: list[float] = []
    monkeypatch.setattr(
        "screener_capacity.builder.GCPBootstrapTokenMinter.mint",
        lambda _self: "bootstrap-" + "x" * 120,
    )
    monkeypatch.setattr("screener_capacity.builder.time.sleep", sleeps.append)
    control = _SourceControl(_source_review())
    targon = _Targon()

    assert run_one_source_review(_settings(), targon, control)

    assert targon.created is not None
    assert targon.created["name"] == "ditto-source-550e8400e29b41d4"
    assert targon.created["image"] == _source_review()["image_reference"]
    assert targon.created["resource_name"] == "cpu-small"
    assert targon.created["commands"] == [
        "/app/workers/screener/.venv/bin/python",
        "-m",
    ]
    assert targon.created["args"] == ["ditto_screener.source_review_job"]
    envs = targon.created["envs"]
    assert {row["name"] for row in envs} >= {
        "DITTO_SOURCE_REVIEW_JOB",
        "DITTO_SOURCE_REVIEW_JOB_TOKEN",
        "SCREENER_GCP_BOOTSTRAP_ACCESS_TOKEN",
        "SCREENER_SOURCE_REVIEW_SECRET_RESOURCE",
    }
    assert targon.deployed == ["wrk-build-1"]
    assert targon.deleted == ["wrk-build-1"]
    assert targon.updated == []
    assert sleeps == [5]


def test_source_review_capacity_miss_falls_back_without_rental() -> None:
    control = _SourceControl(_source_review())
    targon = _Targon(available=0)

    assert run_one_source_review(_settings(), targon, control)

    assert targon.created is None
    assert control.updates[-1][1]["error_code"] == (
        "TARGON_SOURCE_REVIEW_CAPACITY_UNAVAILABLE"
    )


def test_runtime_smoke_launches_promoted_image_directly(monkeypatch) -> None:
    build_id = "550e8400-e29b-41d4-a716-446655440000"
    artifact = {
        "build_id": build_id,
        "archive_url_b64": base64.b64encode(
            b"https://storage.example/image.tar"
        ).decode(),
        "output_sha256": "c" * 64,
        "output_size_bytes": 123,
        "destination": "registry.example/candidates/miner:build-test",
    }
    control = _SubmissionControl(artifact)
    targon = _Targon()
    monkeypatch.setattr(
        "screener_capacity.builder._download_runtime_archive",
        lambda _artifact, destination: destination.write_bytes(b"image"),
    )
    minted_accounts: list[str] = []

    def _mint_access_token(service_account: str) -> str:
        minted_accounts.append(service_account)
        return "registry-" + service_account + "-" + "x" * 120

    monkeypatch.setattr(
        "screener_capacity.builder._mint_access_token", _mint_access_token
    )
    image = "registry.example/candidates/miner@sha256:" + "d" * 64
    promotion: dict[str, object] = {}

    def _promote_runtime_archive(**values: object) -> str:
        promotion.update(values)
        return image

    monkeypatch.setattr(
        "screener_capacity.builder._promote_runtime_archive",
        _promote_runtime_archive,
    )

    class _Health:
        status = 200

        def __enter__(self):  # type: ignore[no-untyped-def]
            return self

        def __exit__(self, *_args):  # type: ignore[no-untyped-def]
            return False

    monkeypatch.setattr(
        "screener_capacity.builder.urllib.request.urlopen",
        lambda *_args, **_kwargs: _Health(),
    )

    assert run_one_runtime_smoke(_settings(), targon, control)

    assert targon.created is not None
    assert targon.created["image"] == image
    assert targon.created["ports"] == [
        {"port": 8080, "protocol": "TCP", "routing": "PROXIED"}
    ]
    assert minted_accounts == ["builder@example.test", "candidate@example.test"]
    assert promotion["access_token"] == ("registry-builder@example.test-" + "x" * 120)
    assert targon.created["registry_auth"]["password"] == (
        "registry-candidate@example.test-" + "x" * 120
    )
    assert control.updates[-1][1]["status"] == "succeeded"
    assert control.updates[-1][1]["image_reference"] == image


def test_runtime_smoke_delete_failure_is_suspended_and_audited(monkeypatch) -> None:
    build_id = "550e8400-e29b-41d4-a716-446655440000"
    artifact = {
        "build_id": build_id,
        "archive_url_b64": base64.b64encode(
            b"https://storage.example/image.tar"
        ).decode(),
        "output_sha256": "c" * 64,
        "output_size_bytes": 123,
        "destination": "registry.example/candidates/miner:build-test",
    }
    control = _SubmissionControl(artifact)
    targon = _Targon(delete_fails=True)
    monkeypatch.setattr(
        "screener_capacity.builder._download_runtime_archive",
        lambda _artifact, destination: destination.write_bytes(b"image"),
    )
    monkeypatch.setattr(
        "screener_capacity.builder._mint_access_token",
        lambda _service_account: "registry-" + "x" * 120,
    )
    monkeypatch.setattr(
        "screener_capacity.builder._promote_runtime_archive",
        lambda **_values: "registry.example/candidates/miner@sha256:" + "d" * 64,
    )

    class _Health:
        status = 200

        def __enter__(self):  # type: ignore[no-untyped-def]
            return self

        def __exit__(self, *_args):  # type: ignore[no-untyped-def]
            return False

    monkeypatch.setattr(
        "screener_capacity.builder.urllib.request.urlopen",
        lambda *_args, **_kwargs: _Health(),
    )

    assert run_one_runtime_smoke(_settings(), targon, control)

    assert targon.deleted == ["wrk-build-1"]
    assert targon.suspended == ["wrk-build-1"]
    assert control.cleanup == [(build_id, "wrk-build-1")]


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
    assert targon.suspended == []
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

    assert targon.deleted == ["wrk-build-1"]
    assert targon.suspended == ["wrk-build-1"]
    assert control.cleanup == [(_submission()["build_id"], "wrk-build-1")]


def test_delete_lost_response_reconciles_provider_deleted_state() -> None:
    targon = _Targon(delete_fails=True, state_after_delete_failure="deleted")

    assert _delete_rental(targon, "wrk-build-1")

    assert targon.deleted == ["wrk-build-1"]
    assert targon.suspended == []


def test_submission_deploy_failure_is_distinct_from_runtime_failure() -> None:
    control = _SubmissionControl(_submission())
    targon = _Targon(
        deploy_error=TargonAPIError(
            operation="POST deploy", status=503, reason="HTTP error"
        )
    )

    assert run_one_submission(_settings(), targon, control)

    assert control.updates[-1] == (
        _submission()["build_id"],
        {
            "status": "fallback_required",
            "provider_resource_id": "wrk-build-1",
            "error_code": "TARGON_SUBMISSION_DEPLOY_ERROR",
        },
    )


def test_submission_runtime_uses_public_safe_builder_stage() -> None:
    control = _SubmissionControl(_submission(), status="running")
    targon = _Targon(
        state_status="error",
        state_message="Container failed (Error) — exit code 72",
    )

    assert run_one_submission(_settings(), targon, control)

    assert control.updates[-1] == (
        _submission()["build_id"],
        {
            "status": "fallback_required",
            "provider_resource_id": "wrk-build-1",
            "error_code": "TARGON_SUBMISSION_KANIKO_FAILED",
        },
    )


def test_submission_runtime_without_marker_stays_provider_specific() -> None:
    control = _SubmissionControl(_submission(), status="running")
    targon = _Targon(state_status="error")

    assert run_one_submission(_settings(), targon, control)

    assert control.updates[-1][1]["error_code"] == "TARGON_SUBMISSION_RUNTIME_ERROR"


def test_submission_update_reconciles_a_lost_platform_response(monkeypatch) -> None:
    requests: list[str] = []

    def request(method: str, *_args: Any, **_kwargs: Any) -> dict[str, Any]:
        requests.append(method)
        if method == "PUT":
            raise ControllerError("Platform trusted-build transport failed")
        return {"status": "fallback_required"}

    monkeypatch.setattr("screener_capacity.builder._request", request)
    control = SubmissionBuildControl(
        platform_url="https://platform.example",
        token="x" * 40,
        environment="prod",
        epoch="builder:test",
    )

    control.update(
        _submission()["build_id"],
        status="fallback_required",
        error_code="TARGON_SUBMISSION_RUNTIME_ERROR",
    )

    assert requests == ["PUT", "GET"]
