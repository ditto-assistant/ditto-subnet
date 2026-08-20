from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import UUID, uuid4

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from ditto.api_models.agent_status import AgentStatus
from ditto.api_server.config import TargonRentalConfig
from ditto.api_server.screening_provider import BuildSpec, ReviewSpec, SmokeSpec
from ditto.api_server.targon_provider import TargonComputeProvider
from ditto.api_server.targon_rental_loop import TargonRentalLoop
from ditto.db.models import Agent, SubmissionImageBuild, SubmissionSourceReview
from ditto.tests.api_server.endpoints.test_screener import (
    _SCREENER_HOTKEY,
    _seed_agent,
)


class _FakeTargon:
    def __init__(self, *, status: str = "running") -> None:
        self.status = status
        self.status_by_uid: dict[str, str] = {}
        self.created: list[dict[str, Any]] = []
        self.deployed: list[str] = []
        self.deleted: list[str] = []

    async def inventory(self) -> list[dict[str, Any]]:
        return [{"name": "cpu-small", "available": 1}]

    async def create_rental(self, **payload: Any) -> dict[str, Any]:
        self.created.append(payload)
        return {"uid": f"wrk-{len(self.created)}"}

    async def deploy(self, uid: str) -> None:
        self.deployed.append(uid)

    async def state(self, uid: str) -> dict[str, Any]:
        return {
            "status": self.status_by_uid.get(uid, self.status),
            "urls": [{"port": 8080, "url": "https://runtime.example"}],
        }

    async def delete(self, uid: str) -> None:
        self.deleted.append(uid)


def _config(**overrides: Any) -> TargonRentalConfig:
    values: dict[str, Any] = {
        "api_key": "k" * 32,
        "org_slug": "ditto",
        "resource": "cpu-small",
        "public_platform_url": "https://platform-api.heyditto.ai",
        "submission_builder_image": (
            "us-central1-docker.pkg.dev/ditto-app-dev/"
            "ditto-public-builders/submission-builder@sha256:" + "ab" * 32
        ),
        "candidate_writer_sa": "push@example.test",
        "candidate_reader_sa": "pull@example.test",
        "bootstrap_sa": "boot@example.test",
        "source_review_secret_resource": "projects/p/secrets/s",
        "runtime_timeout_seconds": 1,
    }
    values.update(overrides)
    return TargonRentalConfig(**values)


@pytest.mark.asyncio
async def test_tick_admits_and_launches_kaniko(
    session_maker: async_sessionmaker[AsyncSession],
) -> None:
    agent_id = await _seed_agent(session_maker, status=AgentStatus.UPLOADED)
    targon = _FakeTargon()
    loop = TargonRentalLoop(
        session_maker=session_maker,
        config=_config(),
        targon=targon,
        screener_hotkey=_SCREENER_HOTKEY,
        interval_seconds=60,
    )

    assert await loop.tick() is True
    assert len(targon.created) == 1
    env = {row["name"]: row["value"] for row in targon.created[0]["envs"]}
    assert env["DITTO_PLATFORM_URL"] == "https://platform-api.heyditto.ai"
    assert env["DITTO_BUILD_JOB_TOKEN"]
    assert targon.deployed == ["wrk-1"]
    async with session_maker() as session:
        agent = await session.get(Agent, agent_id)
        assert agent is not None
        assert agent.status == AgentStatus.SCREENING
        build = await session.scalar(select(SubmissionImageBuild).limit(1))
        assert build is not None
        assert build.status == "running"
        assert UUID(env["DITTO_BUILD_ID"]) == build.build_id


@pytest.mark.asyncio
async def test_tick_launches_runtime_smoke_after_kaniko_archive(
    session_maker: async_sessionmaker[AsyncSession],
) -> None:
    await _seed_agent(session_maker, status=AgentStatus.UPLOADED)
    targon = _FakeTargon()
    promoted: list[tuple[str, str]] = []

    async def promote(key: str, destination: str, _writer: str) -> str:
        promoted.append((key, destination))
        return (
            "us-central1-docker.pkg.dev/ditto-app-dev/"
            "ditto-screening-candidates/miner@sha256:" + "cd" * 32
        )

    async def mint(_sa: str) -> str:
        return "token-" + "x" * 120

    async def health(_url: str) -> bool:
        return True

    loop = TargonRentalLoop(
        session_maker=session_maker,
        config=_config(),
        targon=targon,
        screener_hotkey=_SCREENER_HOTKEY,
        promote_archive=promote,
        mint_token=mint,
        health_probe=health,
        interval_seconds=60,
    )
    await loop.tick()
    async with session_maker() as session, session.begin():
        build = await session.scalar(select(SubmissionImageBuild).limit(1))
        assert build is not None
        build.status = "succeeded"
        build.output_sha256 = "12" * 32
        build.output_size_bytes = 123
        build.output_key = f"remote-builds/{build.build_id}/image.tar"
        build.runtime_status = "pending"
        build.completed_at = datetime.now(UTC)

    assert await loop.tick() is True
    assert promoted
    runtime = targon.created[-1]
    assert runtime["ports"] == [{"port": 8080, "protocol": "TCP", "routing": "PROXIED"}]
    async with session_maker() as session:
        build = await session.scalar(select(SubmissionImageBuild).limit(1))
        assert build is not None
        assert build.runtime_status == "succeeded"
        assert build.runtime_provider_resource_id is None
    assert "wrk-2" in targon.deleted


@pytest.mark.asyncio
async def test_reaps_terminal_kaniko_rental_and_skips_running(
    session_maker: async_sessionmaker[AsyncSession],
) -> None:
    await _seed_agent(session_maker, status=AgentStatus.UPLOADED)
    targon = _FakeTargon()
    loop = TargonRentalLoop(
        session_maker=session_maker,
        config=_config(),
        targon=targon,
        screener_hotkey=_SCREENER_HOTKEY,
        interval_seconds=60,
    )
    await loop.tick()
    assert targon.deleted == []
    async with session_maker() as session, session.begin():
        build = await session.scalar(select(SubmissionImageBuild).limit(1))
        assert build is not None
        build.status = "succeeded"
        build.completed_at = datetime.now(UTC)
    assert await loop.tick() is True
    assert targon.deleted == ["wrk-1"]
    async with session_maker() as session:
        build = await session.scalar(select(SubmissionImageBuild).limit(1))
        assert build is not None
        assert build.provider_resource_id is None


@pytest.mark.asyncio
async def test_reaps_terminal_source_review_rental(
    session_maker: async_sessionmaker[AsyncSession],
) -> None:
    await _seed_agent(session_maker, status=AgentStatus.UPLOADED)
    targon = _FakeTargon()
    loop = TargonRentalLoop(
        session_maker=session_maker,
        config=_config(),
        targon=targon,
        screener_hotkey=_SCREENER_HOTKEY,
        interval_seconds=60,
    )
    await loop.tick()
    async with session_maker() as session, session.begin():
        build = await session.scalar(select(SubmissionImageBuild).limit(1))
        assert build is not None
        session.add(
            SubmissionSourceReview(
                review_id=uuid4(),
                agent_id=build.agent_id,
                attempt_id=build.attempt_id,
                environment="prod",
                artifact_sha256=build.artifact_sha256,
                status="succeeded",
                provider="targon",
                provider_resource_id="wrk-source-leftover",
            )
        )
    targon.deleted.clear()
    assert await loop.tick() is True
    assert "wrk-source-leftover" in targon.deleted
    async with session_maker() as session:
        review = await session.scalar(select(SubmissionSourceReview).limit(1))
        assert review is not None
        assert review.provider_resource_id is None


@pytest.mark.asyncio
async def test_launches_queued_kaniko_in_parallel(
    session_maker: async_sessionmaker[AsyncSession],
) -> None:
    await _seed_agent(
        session_maker, status=AgentStatus.UPLOADED, name="one", sha256="aa" * 32
    )
    await _seed_agent(
        session_maker, status=AgentStatus.UPLOADED, name="two", sha256="bb" * 32
    )
    await _seed_agent(
        session_maker, status=AgentStatus.UPLOADED, name="three", sha256="cc" * 32
    )
    targon = _FakeTargon()
    loop = TargonRentalLoop(
        session_maker=session_maker,
        config=_config(),
        targon=targon,
        screener_hotkey=_SCREENER_HOTKEY,
        interval_seconds=60,
    )
    await loop.tick()
    await loop.tick()
    await loop.tick()
    assert len(targon.created) == 3


@pytest.mark.asyncio
async def test_reaps_expired_running_kaniko_rental(
    session_maker: async_sessionmaker[AsyncSession],
) -> None:
    await _seed_agent(session_maker, status=AgentStatus.UPLOADED)
    targon = _FakeTargon()
    loop = TargonRentalLoop(
        session_maker=session_maker,
        config=_config(),
        targon=targon,
        screener_hotkey=_SCREENER_HOTKEY,
        interval_seconds=60,
    )
    await loop.tick()
    async with session_maker() as session, session.begin():
        build = await session.scalar(select(SubmissionImageBuild).limit(1))
        assert build is not None
        build.updated_at = datetime.now(UTC) - timedelta(hours=2)
        build.lease_expires_at = datetime.now(UTC) - timedelta(minutes=1)
    targon.deleted.clear()
    assert await loop.tick() is True
    assert "wrk-1" in targon.deleted
    async with session_maker() as session:
        build = await session.scalar(select(SubmissionImageBuild).limit(1))
        assert build is not None
        assert build.status == "fallback_required"
        assert build.provider_resource_id is None


@pytest.mark.asyncio
async def test_kaniko_provision_timeout_deletes_rental(
    session_maker: async_sessionmaker[AsyncSession],
) -> None:
    await _seed_agent(session_maker, status=AgentStatus.UPLOADED)
    targon = _FakeTargon(status="provisioning")
    loop = TargonRentalLoop(
        session_maker=session_maker,
        config=_config(provision_timeout_seconds=0),
        targon=targon,
        screener_hotkey=_SCREENER_HOTKEY,
        interval_seconds=60,
    )
    assert await loop.tick() is True
    assert targon.deployed == ["wrk-1"]
    assert targon.deleted == ["wrk-1"]
    async with session_maker() as session:
        build = await session.scalar(select(SubmissionImageBuild).limit(1))
        assert build is not None
        assert build.status == "fallback_required"
        assert build.error_code == "TARGON_PROVISION_TIMEOUT"
        assert build.provider_resource_id is None


@pytest.mark.asyncio
async def test_kaniko_provision_error_deletes_rental(
    session_maker: async_sessionmaker[AsyncSession],
) -> None:
    await _seed_agent(session_maker, status=AgentStatus.UPLOADED)
    targon = _FakeTargon(status="error")
    loop = TargonRentalLoop(
        session_maker=session_maker,
        config=_config(provision_timeout_seconds=0),
        targon=targon,
        screener_hotkey=_SCREENER_HOTKEY,
        interval_seconds=60,
    )
    assert await loop.tick() is True
    assert targon.deleted == ["wrk-1"]
    async with session_maker() as session:
        build = await session.scalar(select(SubmissionImageBuild).limit(1))
        assert build is not None
        assert build.status == "fallback_required"
        assert build.error_code == "TARGON_PROVISION_ERROR"
        assert build.provider_resource_id is None


@pytest.mark.asyncio
async def test_reaps_stuck_provisioning_kaniko_before_lease(
    session_maker: async_sessionmaker[AsyncSession],
) -> None:
    await _seed_agent(session_maker, status=AgentStatus.UPLOADED)
    targon = _FakeTargon()
    loop = TargonRentalLoop(
        session_maker=session_maker,
        config=_config(),
        targon=targon,
        screener_hotkey=_SCREENER_HOTKEY,
        interval_seconds=60,
    )
    await loop.tick()
    targon.status = "provisioning"
    async with session_maker() as session, session.begin():
        build = await session.scalar(select(SubmissionImageBuild).limit(1))
        assert build is not None
        build.updated_at = datetime.now(UTC) - timedelta(minutes=11)
        build.lease_expires_at = datetime.now(UTC) + timedelta(minutes=40)
    targon.deleted.clear()
    assert await loop.tick() is True
    assert "wrk-1" in targon.deleted
    async with session_maker() as session:
        build = await session.scalar(select(SubmissionImageBuild).limit(1))
        assert build is not None
        assert build.status == "fallback_required"
        assert build.error_code == "TARGON_PROVISION_TIMEOUT"
        assert build.provider_resource_id is None


@pytest.mark.asyncio
async def test_does_not_reap_compiling_kaniko_after_provision_window(
    session_maker: async_sessionmaker[AsyncSession],
) -> None:
    await _seed_agent(session_maker, status=AgentStatus.UPLOADED)
    targon = _FakeTargon()
    loop = TargonRentalLoop(
        session_maker=session_maker,
        config=_config(),
        targon=targon,
        screener_hotkey=_SCREENER_HOTKEY,
        interval_seconds=60,
    )
    await loop.tick()
    async with session_maker() as session, session.begin():
        build = await session.scalar(select(SubmissionImageBuild).limit(1))
        assert build is not None
        build.updated_at = datetime.now(UTC) - timedelta(minutes=11)
        build.lease_expires_at = datetime.now(UTC) + timedelta(minutes=40)
    targon.deleted.clear()
    await loop.tick()
    assert targon.deleted == []
    async with session_maker() as session:
        build = await session.scalar(select(SubmissionImageBuild).limit(1))
        assert build is not None
        assert build.status == "running"
        assert build.provider_resource_id == "wrk-1"


@pytest.mark.asyncio
async def test_runtime_smoke_provision_timeout(
    session_maker: async_sessionmaker[AsyncSession],
) -> None:
    await _seed_agent(session_maker, status=AgentStatus.UPLOADED)
    targon = _FakeTargon()

    async def promote(key: str, destination: str, _writer: str) -> str:
        del key, destination
        return (
            "us-central1-docker.pkg.dev/ditto-app-dev/"
            "ditto-screening-candidates/miner@sha256:" + "cd" * 32
        )

    async def mint(_sa: str) -> str:
        return "token-" + "x" * 120

    async def health(_url: str) -> bool:
        raise AssertionError("health must not run before the rental is running")

    loop = TargonRentalLoop(
        session_maker=session_maker,
        config=_config(provision_timeout_seconds=0),
        targon=targon,
        screener_hotkey=_SCREENER_HOTKEY,
        promote_archive=promote,
        mint_token=mint,
        health_probe=health,
        interval_seconds=60,
    )
    await loop.tick()
    async with session_maker() as session, session.begin():
        build = await session.scalar(select(SubmissionImageBuild).limit(1))
        assert build is not None
        build.status = "succeeded"
        build.output_sha256 = "12" * 32
        build.output_size_bytes = 123
        build.output_key = f"remote-builds/{build.build_id}/image.tar"
        build.runtime_status = "pending"
        build.completed_at = datetime.now(UTC)
    targon.status = "provisioning"
    targon.deleted.clear()
    assert await loop.tick() is True
    async with session_maker() as session:
        build = await session.scalar(select(SubmissionImageBuild).limit(1))
        assert build is not None
        assert build.runtime_status == "fallback_required"
        assert build.runtime_error_code == "TARGON_PROVISION_TIMEOUT"
        assert build.runtime_provider_resource_id is None
    assert "wrk-2" in targon.deleted


class _FakeCloudRun:
    name = "cloudrun"
    stored_provider = "gcp"

    def __init__(self) -> None:
        self.builds: list[str] = []
        self.started: list[str] = []
        self.deleted: list[str] = []
        self.status = "running"

    async def capacity_ok(self) -> bool:
        return True

    async def create_build(self, spec: BuildSpec) -> str:
        self.builds.append(spec.name)
        return f"job:{spec.name}"

    async def create_smoke(self, spec: SmokeSpec) -> str:
        return f"service:{spec.name}"

    async def create_source_review(self, spec: ReviewSpec) -> str:
        return f"job:{spec.name}"

    async def start(self, resource_id: str) -> None:
        self.started.append(resource_id)

    async def provision_status(self, resource_id: str) -> str:
        del resource_id
        return self.status

    async def wait_until_running(self, resource_id: str, timeout_seconds: float) -> str:
        del resource_id, timeout_seconds
        return "running" if self.status == "running" else "timeout"

    async def probe_smoke(self, resource_id: str, *, timeout_seconds: float) -> bool:
        del resource_id, timeout_seconds
        return self.status == "running"

    async def delete(self, resource_id: str) -> bool:
        self.deleted.append(resource_id)
        return True


@pytest.mark.asyncio
async def test_kaniko_falls_back_to_cloudrun_when_targon_has_no_capacity(
    session_maker: async_sessionmaker[AsyncSession],
) -> None:
    await _seed_agent(session_maker, status=AgentStatus.UPLOADED)

    class _EmptyTargon(_FakeTargon):
        async def inventory(self) -> list[dict[str, Any]]:
            return [{"name": "cpu-small", "available": 0}]

    targon = _EmptyTargon()
    cloudrun = _FakeCloudRun()
    config = _config()
    loop = TargonRentalLoop(
        session_maker=session_maker,
        config=config,
        targon=targon,
        screener_hotkey=_SCREENER_HOTKEY,
        providers=[TargonComputeProvider(targon, config), cloudrun],
        interval_seconds=60,
    )
    assert await loop.tick() is True
    assert targon.created == []
    assert cloudrun.builds
    assert cloudrun.started
    async with session_maker() as session:
        build = await session.scalar(select(SubmissionImageBuild).limit(1))
        assert build is not None
        assert build.provider == "gcp"
        assert build.status == "running"
        assert build.provider_resource_id == f"job:{cloudrun.builds[0]}"


@pytest.mark.asyncio
async def test_kaniko_falls_back_to_cloudrun_after_targon_provision_timeout(
    session_maker: async_sessionmaker[AsyncSession],
) -> None:
    await _seed_agent(session_maker, status=AgentStatus.UPLOADED)
    targon = _FakeTargon(status="provisioning")
    cloudrun = _FakeCloudRun()
    config = _config(provision_timeout_seconds=0)
    loop = TargonRentalLoop(
        session_maker=session_maker,
        config=config,
        targon=targon,
        screener_hotkey=_SCREENER_HOTKEY,
        providers=[TargonComputeProvider(targon, config), cloudrun],
        interval_seconds=60,
    )
    assert await loop.tick() is True
    assert targon.deleted == ["wrk-1"]
    assert cloudrun.builds
    async with session_maker() as session:
        build = await session.scalar(select(SubmissionImageBuild).limit(1))
        assert build is not None
        assert build.provider == "gcp"
        assert build.status == "running"
        assert build.error_code is None
