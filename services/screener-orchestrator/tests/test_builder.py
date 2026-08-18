import base64
import json
from dataclasses import replace
from pathlib import Path
from typing import Any

import pytest

from screener_capacity.builder import (
    Settings,
    SubmissionBuildControl,
    _delete_rental,
    _docker_config,
    _image_archive_sources,
    _kaniko_script,
    _promote_runtime_archive,
    _skopeo_detail,
    build_parser,
    run_one,
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


def _write_tar(path: Path, names: list[str]) -> None:
    import io
    import tarfile

    with tarfile.open(path, "w") as handle:
        for name in names:
            info = tarfile.TarInfo(name)
            payload = b"{}"
            info.size = len(payload)
            handle.addfile(info, io.BytesIO(payload))


def test_image_archive_prefers_oci_layout_then_docker_manifest(tmp_path: Path) -> None:
    oci = tmp_path / "oci.tar"
    docker = tmp_path / "docker.tar"
    _write_tar(oci, ["oci-layout", "index.json", "blobs"])
    _write_tar(docker, ["manifest.json", "sha256:abc"])
    assert _image_archive_sources(oci)[0].startswith("oci-archive:")
    assert _image_archive_sources(docker)[0].startswith("docker-archive:")


class _Proc:
    def __init__(
        self,
        *,
        returncode: int = 0,
        stdout: str = "",
        stderr: str = "",
        args: list[str] | None = None,
    ) -> None:
        self.returncode = returncode
        self.stdout = stdout
        self.stderr = stderr
        self.args = args or []


def test_promote_retries_oci_when_docker_archive_is_rejected(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    archive = tmp_path / "image.tar"
    _write_tar(archive, ["manifest.json"])
    calls: list[list[str]] = []

    def _run(argv: list[str], **_kwargs: object) -> _Proc:
        calls.append(argv)
        if argv[1] == "inspect":
            return _Proc(stdout="sha256:" + "d" * 64 + "\n")
        source = next(
            part
            for part in argv
            if part.startswith(("oci-archive:", "docker-archive:"))
        )
        if argv[1] == "copy" and source.startswith("oci-archive:"):
            return _Proc()
        return _Proc(
            returncode=1,
            stderr="Error parsing image: invalid tar header",
            args=argv,
        )

    monkeypatch.setattr("screener_capacity.builder.subprocess.run", _run)
    reference = _promote_runtime_archive(
        archive=archive,
        destination="us-central1-docker.pkg.dev/p/candidates/miner:build-test",
        access_token="token",
    )
    assert reference.endswith("@sha256:" + "d" * 64)
    copy_cmds = [argv for argv in calls if argv[1] == "copy"]
    assert copy_cmds[0][2:5] == [
        "--dest-username",
        "oauth2accesstoken",
        "--dest-password-stdin",
    ]
    copy_sources = [
        next(
            part
            for part in argv
            if part.startswith(("oci-archive:", "docker-archive:"))
        )
        for argv in copy_cmds
    ]
    assert copy_sources == [
        f"docker-archive:{archive}",
        f"oci-archive:{archive}",
    ]


def test_promote_includes_redacted_skopeo_stderr(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    archive = tmp_path / "image.tar"
    _write_tar(archive, ["manifest.json"])

    def _run(argv: list[str], **_kwargs: object) -> object:
        result = type("Result", (), {})()
        result.returncode = 1
        result.stdout = ""
        result.stderr = "denied ya29.super-secret-token for docker-archive"
        result.args = argv
        return result

    monkeypatch.setattr("screener_capacity.builder.subprocess.run", _run)
    with pytest.raises(ControllerError, match="docker-archive") as raised:
        _promote_runtime_archive(
            archive=archive,
            destination="us-central1-docker.pkg.dev/p/candidates/miner:build-test",
            access_token="token",
        )
    assert "ya29." not in str(raised.value)
    assert "[oauth]" in str(raised.value)


def test_promote_inspect_failure_includes_redacted_skopeo_stderr(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    archive = tmp_path / "image.tar"
    _write_tar(archive, ["manifest.json"])

    def _run(argv: list[str], **_kwargs: object) -> _Proc:
        if argv[1] == "copy":
            return _Proc()
        return _Proc(
            returncode=1,
            stderr="inspect denied ya29.super-secret-token",
            args=argv,
        )

    monkeypatch.setattr("screener_capacity.builder.subprocess.run", _run)
    with pytest.raises(ControllerError, match="inspect denied") as raised:
        _promote_runtime_archive(
            archive=archive,
            destination="us-central1-docker.pkg.dev/p/candidates/miner:build-test",
            access_token="token",
        )
    assert "ya29." not in str(raised.value)
    assert "[oauth]" in str(raised.value)


def test_skopeo_detail_redacts_oauth_noise() -> None:
    error = type("Err", (), {"stderr": "push failed ya29.abcDEF123", "stdout": ""})()
    assert _skopeo_detail(error) == "push failed [oauth]"


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
    assert "sleep 1800" in script
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
        delete_fails_until_suspended: bool = False,
        available: int = 1,
        deploy_error: TargonAPIError | None = None,
        state_status: str = "running",
        state_message: str = "",
        state_after_delete_failure: str | None = None,
    ) -> None:
        self.delete_fails = delete_fails
        self.delete_fails_until_suspended = delete_fails_until_suspended
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
        self.log_text = "DITTO_BUILD_DIGEST=sha256:" + "b" * 64

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
        if self.delete_fails_until_suspended and uid not in self.suspended:
            raise TargonAPIError(operation="DELETE", status=500, reason="HTTP error")

    def logs(self, _uid: str, tail: int = 160) -> str:
        del tail
        return self.log_text


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
        sweep_interval_seconds=300,
        submission_builder_image="public.example/submission-builder@sha256:" + "a" * 64,
        gcp_bootstrap_service_account="bootstrap@example.test",
        gcp_bootstrap_delegate_service_account=None,
        source_review_secret_resource="projects/test/secrets/reviewer",
        source_review_timeout_seconds=1,
        candidate_registry_service_account="candidate-pull@example.test",
        candidate_registry_writer_service_account="candidate-push@example.test",
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
    assert "experiments" not in targon.created
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
    assert minted_accounts == [
        "candidate-push@example.test",
        "candidate-pull@example.test",
    ]
    assert promotion["access_token"] == (
        "registry-candidate-push@example.test-" + "x" * 120
    )
    assert targon.created["registry_auth"]["password"] == (
        "registry-candidate-pull@example.test-" + "x" * 120
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

    assert targon.deleted == ["wrk-build-1", "wrk-build-1"]
    assert targon.suspended == []
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

    assert targon.deleted == ["wrk-build-1", "wrk-build-1"]
    assert targon.suspended == []
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


def _trusted_build() -> dict[str, Any]:
    return {
        "build_id": "550e8400-e29b-41d4-a716-446655440000",
        "source_repository": "https://github.com/ditto-assistant/ditto-subnet.git",
        "source_sha": "a" * 40,
        "dockerfile_path": "workers/screener/Dockerfile",
        "destination": "us-central1-docker.pkg.dev/p/r/screener:sha-a",
    }


def test_trusted_kaniko_rental_uses_public_runtime_writer_only(monkeypatch) -> None:
    minted_accounts: list[str] = []
    monkeypatch.setattr(
        "screener_capacity.builder._mint_access_token",
        lambda service_account: (
            minted_accounts.append(service_account) or ("registry-" + service_account)
        ),
    )
    control = _SubmissionControl(_trusted_build())
    targon = _Targon()

    assert run_one(_settings(), targon, control)

    assert minted_accounts == ["builder@example.test"]
    assert targon.created is not None
    encoded = next(
        row["value"]
        for row in targon.created["envs"]
        if row["name"] == "DITTO_DOCKER_CONFIG_B64"
    )
    config = json.loads(base64.b64decode(encoded))
    assert config["auths"]["us-central1-docker.pkg.dev"]["password"] == (
        "registry-builder@example.test"
    )
    assert control.updates[-1][1]["status"] == "succeeded"
    assert control.cleanup == []


def test_trusted_kaniko_delete_failure_is_suspended_and_audited(
    monkeypatch,
) -> None:
    monkeypatch.setattr(
        "screener_capacity.builder._mint_access_token",
        lambda _service_account: "registry-builder@example.test",
    )
    control = _SubmissionControl(_trusted_build())
    targon = _Targon(delete_fails=True)

    assert run_one(_settings(), targon, control)

    assert targon.deleted == ["wrk-build-1", "wrk-build-1"]
    assert targon.suspended == []
    assert control.cleanup == [(_trusted_build()["build_id"], "wrk-build-1")]


class TestOptionalLaneConfiguration:
    """A code-only deploy must degrade, never crash-loop the daemon.

    The four lane flags live on the Ansible-owned systemd unit while the code
    ships via the release deploy. When they were argparse ``required=True``, a
    release reaching a host with an older unit exited 2/INVALIDARGUMENT on every
    restart, failed the deploy healthcheck, and rolled the whole controller
    back to the previous revision.
    """

    def test_parser_accepts_a_unit_that_predates_the_lane_flags(self) -> None:
        args = build_parser().parse_args(
            [
                "--platform-url",
                "https://platform.example",
                "--platform-token-file",
                "/etc/token",
                "--targon-api-key-file",
                "/etc/targon",
                "--targon-org-slug",
                "ditto",
                "--kaniko-image",
                "kaniko@example",
                "--registry-service-account",
                "builder@example.test",
            ]
        )
        assert args.candidate_registry_service_account is None
        assert args.candidate_registry_writer_service_account is None
        assert args.gcp_bootstrap_service_account is None
        assert args.source_review_secret_resource is None
        assert args.sweep_interval_seconds == 300

    def test_missing_candidate_registry_disables_only_runtime_smoke(self) -> None:
        settings = replace(
            _settings(),
            candidate_registry_service_account=None,
            candidate_registry_writer_service_account=None,
        )
        assert settings.runtime_smoke_enabled is False
        assert settings.source_review_enabled is True
        assert settings.disabled_lanes() == ["runtime-smoke"]

    def test_missing_bootstrap_identity_disables_only_source_review(self) -> None:
        settings = replace(
            _settings(),
            gcp_bootstrap_service_account=None,
            source_review_secret_resource=None,
        )
        assert settings.source_review_enabled is False
        assert settings.runtime_smoke_enabled is True
        assert settings.disabled_lanes() == ["source-review"]

    def test_fully_configured_unit_enables_every_lane(self) -> None:
        settings = _settings()
        assert settings.runtime_smoke_enabled is True
        assert settings.source_review_enabled is True
        assert settings.disabled_lanes() == []

    def test_unconfigured_runtime_smoke_refuses_to_claim(self) -> None:
        settings = replace(_settings(), candidate_registry_writer_service_account=None)

        class ExplodingControl:
            def claim(self) -> dict[str, Any]:
                raise AssertionError("must not claim work it cannot finish")

        with pytest.raises(ControllerError, match="runtime smoke lane"):
            run_one_runtime_smoke(settings, object(), ExplodingControl())  # type: ignore[arg-type]

    def test_unconfigured_source_review_refuses_to_claim(self) -> None:
        settings = replace(_settings(), source_review_secret_resource=None)

        class ExplodingControl:
            def claim(self) -> dict[str, Any]:
                raise AssertionError("must not claim work it cannot finish")

        with pytest.raises(ControllerError, match="source review lane"):
            run_one_source_review(settings, object(), ExplodingControl())  # type: ignore[arg-type]
