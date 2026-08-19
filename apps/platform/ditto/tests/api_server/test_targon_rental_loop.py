from __future__ import annotations

from datetime import UTC, datetime
from typing import Any
from uuid import UUID, uuid4

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from ditto.api_models.agent_status import AgentStatus
from ditto.api_server.config import TargonRentalConfig
from ditto.api_server.targon_rental_loop import TargonRentalLoop
from ditto.db.models import Agent, SubmissionImageBuild, SubmissionSourceReview
from ditto.tests.api_server.endpoints.test_screener import (
    _SCREENER_HOTKEY,
    _seed_agent,
)


class _FakeTargon:
    def __init__(self) -> None:
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
        del uid
        return {
            "status": "running",
            "urls": [{"port": 8080, "url": "https://runtime.example"}],
        }

    async def delete(self, uid: str) -> None:
        self.deleted.append(uid)


def _config() -> TargonRentalConfig:
    return TargonRentalConfig(
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
        runtime_timeout_seconds=1,
    )


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
