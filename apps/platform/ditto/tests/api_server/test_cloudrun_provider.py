from __future__ import annotations

from typing import Any, cast

import pytest

from ditto.api_server.cloudrun_client import AsyncCloudRunClient
from ditto.api_server.cloudrun_provider import CloudRunComputeProvider
from ditto.api_server.config import CloudRunScreeningConfig, TargonRentalConfig


class _FakeCloudRunClient:
    def __init__(
        self,
        execution: dict[str, Any],
        *,
        job: dict[str, Any] | None = None,
        fail_get_execution: bool = False,
    ) -> None:
        self.execution = execution
        self.fail_get_execution = fail_get_execution
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


def _provider(
    execution: dict[str, Any],
    *,
    job: dict[str, Any] | None = None,
    fail_get_execution: bool = False,
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
                execution, job=job, fail_get_execution=fail_get_execution
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
async def test_job_status_timeout_when_execution_stays_pending() -> None:
    provider = _provider({"status": {"runningCount": 0}}, job=_V2_PENDING_JOB)
    status = await provider.wait_until_running("job:ditto-miner-build-test", 0.01)
    assert status == "timeout"
