from __future__ import annotations

from typing import Any, cast

import pytest

from ditto.api_server.cloudrun_client import AsyncCloudRunClient
from ditto.api_server.cloudrun_provider import CloudRunComputeProvider
from ditto.api_server.config import CloudRunScreeningConfig, TargonRentalConfig


class _FakeCloudRunClient:
    def __init__(self, execution: dict[str, Any]) -> None:
        self.execution = execution
        self.job = {
            "latestCreatedExecution": {
                "name": "projects/p/locations/us-central1/jobs/job/executions/ex"
            }
        }

    async def get_job(self, job_id: str) -> dict[str, Any]:
        del job_id
        return self.job

    async def get_execution(self, execution_name: str) -> dict[str, Any]:
        del execution_name
        return self.execution


def _provider(execution: dict[str, Any]) -> CloudRunComputeProvider:
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
        cast(AsyncCloudRunClient, _FakeCloudRunClient(execution)),
        CloudRunScreeningConfig(
            project="ditto-app-dev",
            region="us-central1",
            untrusted_sa_email="untrusted@example.test",
            platform_invoker_sa_email="invoker@example.test",
        ),
        targon,
    )


@pytest.mark.asyncio
async def test_job_status_reads_nested_v2_running_count() -> None:
    provider = _provider(
        {"status": {"runningCount": 1, "startTime": "2026-08-21T07:29:00Z"}}
    )
    assert await provider.provision_status("job:ditto-miner-build-test") == "running"


@pytest.mark.asyncio
async def test_job_status_does_not_treat_nested_running_as_pending() -> None:
    provider = _provider({"status": {"runningCount": 1}})
    status = await provider.wait_until_running("job:ditto-miner-build-test", 0.01)
    assert status == "running"


@pytest.mark.asyncio
async def test_job_status_reads_nested_failed_count() -> None:
    provider = _provider({"status": {"failedCount": 1}})
    assert await provider.provision_status("job:ditto-miner-build-test") == "error"


@pytest.mark.asyncio
async def test_job_status_timeout_when_execution_stays_pending() -> None:
    provider = _provider({"status": {"runningCount": 0}})
    status = await provider.wait_until_running("job:ditto-miner-build-test", 0.01)
    assert status == "timeout"
