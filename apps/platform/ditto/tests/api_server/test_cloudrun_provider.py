from __future__ import annotations

from typing import Any, cast

import httpx
import pytest

from ditto.api_server.cloudrun_client import (
    AsyncCloudRunClient,
    error_reason_from_response,
)
from ditto.api_server.cloudrun_provider import CloudRunComputeProvider
from ditto.api_server.config import CloudRunScreeningConfig, TargonRentalConfig
from ditto.api_server.screening_provider import SmokeSpec, inflight_failure_code


class _FakeCloudRunClient:
    def __init__(
        self,
        execution: dict[str, Any],
        *,
        job: dict[str, Any] | None = None,
        fail_get_execution: bool = False,
        logs: list[str] | None = None,
    ) -> None:
        self.execution = execution
        self.fail_get_execution = fail_get_execution
        self.logs = logs or []
        self.job = job or {
            "status": {
                "latestCreatedExecution": {
                    "name": ("projects/p/locations/us-central1/jobs/job/executions/ex"),
                    "completionStatus": "EXECUTION_RUNNING",
                }
            }
        }

    async def get_job(self, job_id: str) -> dict[str, Any]:
        del job_id
        return self.job

    async def get_execution(self, execution_name: str) -> dict[str, Any]:
        del execution_name
        if self.fail_get_execution:
            raise AssertionError("get_execution must not run for a v2 running ref")
        return self.execution

    async def list_job_logs(self, job_id: str, *, limit: int = 400) -> list[str]:
        del job_id
        return self.logs[-limit:]


def _provider(
    execution: dict[str, Any],
    *,
    job: dict[str, Any] | None = None,
    fail_get_execution: bool = False,
    logs: list[str] | None = None,
) -> CloudRunComputeProvider:
    targon = TargonRentalConfig(
        api_key="k" * 32,
        org_slug="ditto",
        resource="cpu-small",
        public_platform_url="https://platform-api.heyditto.ai",
        submission_builder_image=(
            "us-central1-docker.pkg.dev/ditto-app-dev/"
            "ditto-public-builders/submission-builder@sha256:" + "ab" * 32
        ),
        candidate_writer_sa="push@example.test",
        candidate_reader_sa="pull@example.test",
        bootstrap_sa="boot@example.test",
        source_review_secret_resource="projects/p/secrets/s",
    )
    return CloudRunComputeProvider(
        cast(
            AsyncCloudRunClient,
            _FakeCloudRunClient(
                execution,
                job=job,
                fail_get_execution=fail_get_execution,
                logs=logs,
            ),
        ),
        CloudRunScreeningConfig(
            project="ditto-app-dev",
            region="us-central1",
            untrusted_sa_email="untrusted@example.test",
            platform_invoker_sa_email="invoker@example.test",
        ),
        targon,
    )


_V2_RUNNING_JOB = {
    "status": {
        "latestCreatedExecution": {
            "name": "projects/p/locations/us-central1/jobs/job/executions/ex",
            "completionStatus": "EXECUTION_RUNNING",
        }
    }
}

_V2_PENDING_JOB = {
    "status": {
        "latestCreatedExecution": {
            "name": "projects/p/locations/us-central1/jobs/job/executions/ex",
            "completionStatus": "EXECUTION_PENDING",
        }
    }
}


@pytest.mark.asyncio
async def test_job_status_reads_v2_nested_execution_running() -> None:
    provider = _provider({}, job=_V2_RUNNING_JOB, fail_get_execution=True)
    assert await provider.provision_status("job:ditto-miner-build-test") == "running"


@pytest.mark.asyncio
async def test_job_status_wait_returns_running_from_v2_job_ref() -> None:
    provider = _provider({}, job=_V2_RUNNING_JOB, fail_get_execution=True)
    status = await provider.wait_until_running("job:ditto-miner-build-test", 0.01)
    assert status == "running"


@pytest.mark.asyncio
async def test_job_status_reads_nested_running_count_when_ref_is_pending() -> None:
    provider = _provider(
        {"status": {"runningCount": 1, "startTime": "2026-08-21T07:29:00Z"}},
        job=_V2_PENDING_JOB,
    )
    assert await provider.provision_status("job:ditto-miner-build-test") == "running"


@pytest.mark.asyncio
async def test_job_status_reads_v2_nested_execution_failed() -> None:
    job = {
        "status": {
            "latestCreatedExecution": {
                "name": "projects/p/locations/us-central1/jobs/job/executions/ex",
                "completionStatus": "EXECUTION_FAILED",
            }
        }
    }
    provider = _provider({}, job=job, fail_get_execution=True)
    assert await provider.provision_status("job:ditto-miner-build-test") == "error"


@pytest.mark.asyncio
async def test_observe_failed_job_maps_kaniko_exit() -> None:
    job = {
        "status": {
            "latestCreatedExecution": {
                "name": "projects/p/locations/us-central1/jobs/job/executions/ex",
                "completionStatus": "EXECUTION_FAILED",
            }
        }
    }
    execution = {
        "status": {
            "conditions": [
                {
                    "type": "Completed",
                    "message": "Container called exit(72).",
                }
            ]
        }
    }
    provider = _provider(execution, job=job)
    observation = await provider.observe_provision("job:ditto-miner-build-test")
    assert observation.status == "error"
    assert (
        inflight_failure_code("gcp", observation.status, observation.message)
        == "CLOUDRUN_SUBMISSION_KANIKO_FAILED"
    )


@pytest.mark.asyncio
async def test_observe_failed_job_uses_redacted_log_marker() -> None:
    job = {
        "status": {
            "latestCreatedExecution": {
                "name": "projects/p/locations/us-central1/jobs/job/executions/ex",
                "completionStatus": "EXECUTION_FAILED",
            }
        }
    }
    provider = _provider(
        {"status": {"conditions": []}},
        job=job,
        logs=[
            "api_key=do-not-return",
            "DITTO_SUBMISSION_BUILD_FAILED=KANIKO",
        ],
    )
    observation = await provider.observe_provision("job:ditto-miner-build-test")
    assert "do-not-return" not in observation.message
    assert "api_key=[REDACTED]" in observation.message
    assert (
        inflight_failure_code("gcp", observation.status, observation.message)
        == "CLOUDRUN_SUBMISSION_KANIKO_FAILED"
    )


@pytest.mark.asyncio
async def test_replica_logs_returns_redacted_job_tail() -> None:
    provider = _provider({}, logs=["Bearer private-token", "useful failure"])
    logs = await provider.replica_logs("job:ditto-miner-build-test", tail=2)
    assert "private-token" not in logs
    assert "Bearer [REDACTED]" in logs
    assert "useful failure" in logs


@pytest.mark.asyncio
async def test_job_status_timeout_when_execution_stays_pending() -> None:
    provider = _provider({"status": {"runningCount": 0}}, job=_V2_PENDING_JOB)
    status = await provider.wait_until_running("job:ditto-miner-build-test", 0.01)
    assert status == "timeout"


class _FakeServiceClient:
    def __init__(self) -> None:
        self.created: list[str] = []
        self.kwargs: dict[str, Any] = {}

    async def create_service(self, name: str, **kwargs: Any) -> None:
        self.created.append(name)
        self.kwargs = kwargs


@pytest.mark.asyncio
async def test_create_smoke_ignores_frozen_registry_auth() -> None:
    client = _FakeServiceClient()
    targon = TargonRentalConfig(
        api_key="k" * 32,
        org_slug="ditto",
        resource="cpu-small",
        public_platform_url="https://platform-api.heyditto.ai",
        submission_builder_image=(
            "us-central1-docker.pkg.dev/ditto-app-dev/"
            "ditto-public-builders/submission-builder@sha256:" + "ab" * 32
        ),
        candidate_writer_sa="push@example.test",
        candidate_reader_sa="pull@example.test",
        bootstrap_sa="boot@example.test",
        source_review_secret_resource="projects/p/secrets/s",
    )
    provider = CloudRunComputeProvider(
        cast(AsyncCloudRunClient, client),
        CloudRunScreeningConfig(
            project="ditto-app-dev",
            region="us-central1",
            untrusted_sa_email="untrusted@example.test",
            platform_invoker_sa_email="invoker@example.test",
        ),
        targon,
    )
    spec = SmokeSpec(
        name="ditto-runtime-test",
        image="us-central1-docker.pkg.dev/ditto-app-dev/ditto-screening-candidates/miner:tag",
        env=(("OPENROUTER_API_KEY", "sk-screener-smoke"),),
        registry_auth={
            "server": "us-central1-docker.pkg.dev",
            "username": "oauth2accesstoken",
            "password": "token",
        },
    )
    uid = await provider.create_smoke(spec)
    assert uid == "service:ditto-runtime-test"
    assert client.created == ["ditto-runtime-test"]
    env = dict(client.kwargs["env"])
    assert env["OLLAMA_BASE_URL"] == "http://127.0.0.1:11434"
    assert env["DITTOBENCH_INFERENCE_BASE_URL"] == "http://127.0.0.1:11434"
    sidecar = client.kwargs["sidecar"]
    assert sidecar["name"] == "gateway"
    assert sidecar["command"][0] == "python"


def test_error_reason_from_response_uses_api_message() -> None:
    response = httpx.Response(
        403,
        json={
            "error": {
                "code": 403,
                "message": (
                    "Permission 'artifactregistry.repositories.downloadArtifacts' "
                    "denied on resource '//artifactregistry.googleapis.com/"
                    "projects/ditto-app-dev/locations/us-central1/repositories/"
                    "ditto-screening-candidates'."
                ),
                "status": "PERMISSION_DENIED",
            }
        },
    )
    reason = error_reason_from_response(response)
    assert "downloadArtifacts" in reason
    assert len(reason) <= 300


def test_error_reason_from_response_falls_back_for_empty_body() -> None:
    response = httpx.Response(500)
    assert error_reason_from_response(response) == "HTTP error"
