from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import UUID, uuid4

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from ditto.api_models.agent_status import AgentStatus
from ditto.api_models.screener import SCREENING_POLICY_VERSION
from ditto.api_models.screener_review_settings import ScreenerReviewSettings
from ditto.api_server.config import TargonRentalConfig
from ditto.api_server.screening_provider import (
    BuildSpec,
    ProvisionObservation,
    ReviewSpec,
    SmokeSpec,
    inflight_failure_code,
)
from ditto.api_server.targon_provider import TargonComputeProvider
from ditto.api_server.targon_rental_loop import (
    _SOURCE_LEASE,
    TargonRentalLoop,
    _source_review_layer_env,
)
from ditto.api_server.targon_screening import _LEASE_TTL, _queue_kaniko
from ditto.db.models import (
    Agent,
    ProviderOutageCircuit,
    ScreeningAttempt,
    SubmissionImageBuild,
    SubmissionSourceReview,
)
from ditto.tests.api_server.endpoints.test_screener import (
    _SCREENER_HOTKEY,
    _SHA256,
    _seed_agent,
)


def test_source_review_layer_env_pins_l2_and_l3() -> None:
    env = dict(
        _source_review_layer_env(
            ScreenerReviewSettings(mode="enforce", l3_enabled=True)
        )
    )
    assert env["SCREENER_L2_REVIEW_MODE"] == "enforce"
    assert env["SCREENER_L3_REVIEW_ENABLED"] == "true"
    assert env["SCREENER_L2_REVIEW_MODEL"] == "moonshotai/kimi-k3"


def test_screening_leases_are_bounded_for_provider_fallback() -> None:
    assert timedelta(minutes=45) == _LEASE_TTL
    assert timedelta(minutes=30) == _SOURCE_LEASE


def test_source_review_layer_env_carries_the_l1_verdict_budget() -> None:
    """The rental must inherit the operator's L1 completion budget.

    Without it the job falls back to its own default and an operator raising
    the budget to stop truncated verdicts would change nothing in production.
    """
    env = dict(
        _source_review_layer_env(
            ScreenerReviewSettings(
                mode="enforce", source_review_max_completion_tokens=12_000
            )
        )
    )
    assert env["SCREENER_SOURCE_REVIEW_MAX_COMPLETION_TOKENS"] == "12000"


class _FakeTargon:
    def __init__(
        self,
        *,
        status: str = "running",
        message: str = "",
        ready_replicas: int = 1,
        total_replicas: int = 1,
    ) -> None:
        self.status = status
        self.message = message
        self.ready_replicas = ready_replicas
        self.total_replicas = total_replicas
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
            "message": getattr(self, "message", ""),
            "ready_replicas": self.ready_replicas,
            "total_replicas": self.total_replicas,
            "urls": [{"port": 8080, "url": "https://runtime.example"}],
        }

    async def logs(self, uid: str, *, tail: int = 400) -> str:
        del uid, tail
        return "kaniko: rustc oom"

    async def delete(self, uid: str) -> None:
        self.deleted.append(uid)


def test_inflight_failure_code_maps_kaniko_exit() -> None:
    assert (
        inflight_failure_code(
            "targon",
            "error",
            "Container failed (Error) — exit code 72",
        )
        == "TARGON_SUBMISSION_KANIKO_FAILED"
    )
    assert (
        inflight_failure_code(
            "gcp",
            "error",
            "Container called exit(72).",
        )
        == "CLOUDRUN_SUBMISSION_KANIKO_FAILED"
    )
    assert inflight_failure_code("targon", "error", "") == "TARGON_PROVISION_ERROR"
    assert inflight_failure_code("gcp", "error", "") == "CLOUDRUN_PROVISION_ERROR"
    assert inflight_failure_code("targon", "timeout") == "TARGON_PROVISION_TIMEOUT"


def test_inflight_failure_code_maps_stable_builder_marker() -> None:
    assert (
        inflight_failure_code(
            "gcp",
            "error",
            "DITTO_SUBMISSION_BUILD_FAILED=KANIKO",
        )
        == "CLOUDRUN_SUBMISSION_KANIKO_FAILED"
    )


@pytest.mark.asyncio
async def test_targon_running_without_replicas_is_not_ready() -> None:
    targon = _FakeTargon(ready_replicas=0, total_replicas=0)
    provider = TargonComputeProvider(targon, _config())
    observation = await provider.observe_provision("wrk-1")
    assert observation.status == "running"
    assert observation.ready is False
    assert await provider.wait_until_running("wrk-1", 0.01) == "timeout"


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
        review = await session.scalar(select(SubmissionSourceReview).limit(1))
        assert review is not None
        assert review.attempt_id == build.attempt_id
        assert review.status == "queued"
        assert review.created_at <= build.updated_at


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
        review = await session.scalar(select(SubmissionSourceReview).limit(1))
        assert review is not None
        review.status = "succeeded"
        review.provider = "targon"
        review.provider_resource_id = "wrk-source-leftover"
    targon.deleted.clear()
    assert await loop.tick() is True
    assert "wrk-source-leftover" in targon.deleted
    async with session_maker() as session:
        review = await session.scalar(select(SubmissionSourceReview).limit(1))
        assert review is not None
        assert review.provider_resource_id is None


@pytest.mark.asyncio
async def test_cancels_source_review_when_parent_attempt_is_terminal(
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
        review = await session.scalar(select(SubmissionSourceReview).limit(1))
        assert review is not None
        attempt = await session.get(ScreeningAttempt, review.attempt_id)
        assert attempt is not None
        review.status = "running"
        review.provider = "targon"
        review.provider_resource_id = "wrk-source-orphan"
        review.job_token_hash = "ab" * 32
        review.job_token_expires_at = datetime.now(UTC) + timedelta(minutes=30)
        attempt.status = "failed"
        attempt.finished_at = datetime.now(UTC)

    targon.deleted.clear()
    assert await loop.tick() is True
    assert targon.deleted == ["wrk-source-orphan"]
    async with session_maker() as session:
        review = await session.scalar(select(SubmissionSourceReview).limit(1))
        assert review is not None
        assert review.status == "canceled"
        assert review.error_code == "SCREENING_ATTEMPT_TERMINAL"
        assert review.provider_resource_id is None
        assert review.job_token_hash is None
        assert review.job_token_expires_at is None
        assert review.completed_at is not None


@pytest.mark.asyncio
async def test_open_provider_circuit_parks_and_deletes_source_review_rental(
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
    epoch = uuid4()
    now = datetime.now(UTC)
    async with session_maker() as session, session.begin():
        build = await session.scalar(select(SubmissionImageBuild).limit(1))
        assert build is not None
        review = await session.scalar(
            select(SubmissionSourceReview).where(
                SubmissionSourceReview.attempt_id == build.attempt_id
            )
        )
        assert review is not None
        review.status = "running"
        review.provider = "targon"
        review.provider_resource_id = "wrk-source-overload"
        review.attempt_count = 2
        review.lease_expires_at = now + timedelta(minutes=30)
        review.job_token_hash = "ab" * 32
        review.job_token_expires_at = now + timedelta(minutes=30)
        session.add(
            ProviderOutageCircuit(
                provider="openrouter",
                state="open",
                epoch=epoch,
                opened_at=now,
                retry_at=now + timedelta(minutes=2),
                last_failure_at=now,
                failure_count=1,
                last_status=429,
                last_error_code="upstream_http_429",
                updated_at=now,
            )
        )

    assert await loop._park_source_reviews_for_outage() is True
    assert "wrk-source-overload" in targon.deleted
    async with session_maker() as session:
        review = await session.scalar(
            select(SubmissionSourceReview).where(
                SubmissionSourceReview.provider_outage_epoch == epoch
            )
        )
        assert review is not None
        assert review.status == "queued"
        assert review.attempt_count == 2
        assert review.provider_resource_id is None
        assert review.job_token_hash is None

    async with session_maker() as session, session.begin():
        review = await session.scalar(
            select(SubmissionSourceReview).where(
                SubmissionSourceReview.provider_outage_epoch == epoch
            )
        )
        assert review is not None
        review.status = "running"
        review.provider_resource_id = "wrk-source-repeat-overload"
        review.provider_outage_attempted_epoch = epoch
        circuit = await session.get(ProviderOutageCircuit, "openrouter")
        assert circuit is not None
        circuit.epoch = uuid4()
    assert await loop._park_source_reviews_for_outage() is True
    async with session_maker() as session:
        review = await session.scalar(select(SubmissionSourceReview).limit(1))
        assert review is not None
        assert review.provider_outage_epoch is None


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
async def test_reaps_expired_leased_kaniko_without_resource_id(
    session_maker: async_sessionmaker[AsyncSession],
) -> None:
    await _seed_agent(
        session_maker, status=AgentStatus.UPLOADED, name="zombie", sha256="aa" * 32
    )
    targon = _FakeTargon()
    loop = TargonRentalLoop(
        session_maker=session_maker,
        config=_config(max_inflight=1),
        targon=targon,
        screener_hotkey=_SCREENER_HOTKEY,
        interval_seconds=60,
    )
    await loop.tick()
    async with session_maker() as session, session.begin():
        build = await session.scalar(select(SubmissionImageBuild).limit(1))
        assert build is not None
        build.status = "leased"
        build.provider = "targon"
        build.provider_resource_id = None
        build.updated_at = datetime.now(UTC) - timedelta(hours=18)
        build.lease_expires_at = datetime.now(UTC) - timedelta(hours=17)
    await _seed_agent(
        session_maker, status=AgentStatus.UPLOADED, name="next", sha256="bb" * 32
    )
    targon.created.clear()
    targon.deployed.clear()
    assert await loop.tick() is True
    async with session_maker() as session:
        builds = (await session.scalars(select(SubmissionImageBuild))).all()
        by_name = {}
        for build in builds:
            agent = await session.get(Agent, build.agent_id)
            assert agent is not None
            by_name[agent.name] = build
        assert by_name["zombie"].status == "fallback_required"
        assert by_name["zombie"].error_code == "TARGON_SUBMISSION_LEASE_EXPIRED"
        assert by_name["next"].status == "running"
        assert by_name["next"].provider_resource_id == "wrk-1"
    assert len(targon.created) == 1


@pytest.mark.asyncio
async def test_reaps_runtime_running_without_resource_after_provision_window(
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
        build.status = "succeeded"
        build.provider = "targon"
        build.output_sha256 = "12" * 32
        build.output_size_bytes = 123
        build.runtime_status = "running"
        build.runtime_provider_resource_id = None
        build.updated_at = datetime.now(UTC) - timedelta(minutes=11)
        build.completed_at = datetime.now(UTC) - timedelta(minutes=11)
    targon.created.clear()
    assert await loop.tick() is True
    async with session_maker() as session:
        build = await session.scalar(select(SubmissionImageBuild).limit(1))
        assert build is not None
        assert build.runtime_status == "fallback_required"
        assert build.runtime_error_code == "TARGON_PROVISION_TIMEOUT"
        assert build.runtime_provider_resource_id is None
    assert targon.created == []


@pytest.mark.asyncio
async def test_reaps_errored_running_kaniko_before_provision_window(
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
    targon.status = "error"
    targon.message = "Container failed (Error) — exit code 72"
    targon.deleted.clear()
    assert await loop.tick() is True
    assert "wrk-1" in targon.deleted
    async with session_maker() as session:
        build = await session.scalar(select(SubmissionImageBuild).limit(1))
        assert build is not None
        assert build.status == "fallback_required"
        assert build.error_code == "TARGON_SUBMISSION_KANIKO_FAILED"
        assert build.provider_resource_id is None


@pytest.mark.asyncio
async def test_does_not_timeout_provisioning_kaniko_inside_window(
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
    targon.deleted.clear()
    await loop.tick()
    assert targon.deleted == []
    async with session_maker() as session:
        build = await session.scalar(select(SubmissionImageBuild).limit(1))
        assert build is not None
        assert build.status == "running"
        assert build.provider_resource_id == "wrk-1"


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
        config=_config(smoke_provision_timeout_seconds=0),
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


@pytest.mark.asyncio
async def test_runtime_smoke_parks_after_short_targon_timeout(
    session_maker: async_sessionmaker[AsyncSession],
) -> None:
    await _seed_agent(session_maker, status=AgentStatus.UPLOADED)
    targon = _FakeTargon()
    cloudrun = _FakeCloudRun()

    async def promote(key: str, destination: str, _writer: str) -> str:
        del key, destination
        return (
            "us-central1-docker.pkg.dev/ditto-app-dev/"
            "ditto-screening-candidates/miner@sha256:" + "cd" * 32
        )

    async def mint(_sa: str) -> str:
        return "token-" + "x" * 120

    loop = TargonRentalLoop(
        session_maker=session_maker,
        config=_config(smoke_provision_timeout_seconds=0),
        targon=targon,
        screener_hotkey=_SCREENER_HOTKEY,
        promote_archive=promote,
        mint_token=mint,
        providers=[TargonComputeProvider(targon, _config()), cloudrun],
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
    assert cloudrun.smokes == []
    assert "wrk-2" in targon.deleted


@pytest.mark.asyncio
async def test_runtime_smoke_parks_running_without_resource(
    session_maker: async_sessionmaker[AsyncSession],
) -> None:
    await _seed_agent(session_maker, status=AgentStatus.UPLOADED)
    targon = _FakeTargon()
    cloudrun = _FakeCloudRun()

    async def promote(key: str, destination: str, _writer: str) -> str:
        del key, destination
        return (
            "us-central1-docker.pkg.dev/ditto-app-dev/"
            "ditto-screening-candidates/miner@sha256:" + "cd" * 32
        )

    async def mint(_sa: str) -> str:
        return "token-" + "x" * 120

    loop = TargonRentalLoop(
        session_maker=session_maker,
        config=_config(provision_timeout_seconds=1),
        targon=targon,
        screener_hotkey=_SCREENER_HOTKEY,
        promote_archive=promote,
        mint_token=mint,
        providers=[TargonComputeProvider(targon, _config()), cloudrun],
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
        build.runtime_status = "running"
        build.runtime_provider_resource_id = None
        build.completed_at = datetime.now(UTC)
        build.updated_at = datetime.now(UTC)
    targon.status = "provisioning"
    assert await loop.tick() is True
    async with session_maker() as session:
        build = await session.scalar(select(SubmissionImageBuild).limit(1))
        assert build is not None
        assert build.runtime_status == "fallback_required"
        assert build.runtime_error_code == "TARGON_PROVISION_TIMEOUT"
    assert cloudrun.smokes == []


class _FakeCloudRun:
    name = "cloudrun"
    stored_provider = "gcp"

    def __init__(self) -> None:
        self.builds: list[str] = []
        self.smokes: list[str] = []
        self.started: list[str] = []
        self.deleted: list[str] = []
        self.status = "running"

    async def capacity_ok(self) -> bool:
        return True

    async def create_build(self, spec: BuildSpec) -> str:
        self.builds.append(spec.name)
        return f"job:{spec.name}"

    async def create_smoke(self, spec: SmokeSpec) -> str:
        self.smokes.append(spec.name)
        return f"service:{spec.name}"

    async def create_source_review(self, spec: ReviewSpec) -> str:
        return f"job:{spec.name}"

    async def start(self, resource_id: str) -> None:
        self.started.append(resource_id)

    async def provision_status(self, resource_id: str) -> str:
        return (await self.observe_provision(resource_id)).status

    async def observe_provision(self, resource_id: str) -> ProvisionObservation:
        del resource_id
        return ProvisionObservation(status=self.status)

    async def wait_until_running(self, resource_id: str, timeout_seconds: float) -> str:
        del resource_id, timeout_seconds
        return "running" if self.status == "running" else "timeout"

    async def probe_smoke(self, resource_id: str, *, timeout_seconds: float) -> bool:
        del resource_id, timeout_seconds
        return self.status == "running"

    async def delete(self, resource_id: str) -> bool:
        self.deleted.append(resource_id)
        return True

    async def replica_logs(self, resource_id: str, *, tail: int = 400) -> str:
        del resource_id, tail
        return ""


@pytest.mark.asyncio
async def test_kaniko_waits_before_dispatch_when_targon_has_no_capacity(
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
    assert await loop.tick() is False
    assert targon.created == []
    assert cloudrun.builds == []
    assert cloudrun.started == []
    async with session_maker() as session:
        build = await session.scalar(select(SubmissionImageBuild).limit(1))
        assert build is not None
        assert build.provider is None
        assert build.status == "queued"
        assert build.provider_resource_id is None


@pytest.mark.asyncio
async def test_kaniko_parks_after_targon_provision_timeout(
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
    assert cloudrun.builds == []
    async with session_maker() as session:
        build = await session.scalar(select(SubmissionImageBuild).limit(1))
        assert build is not None
        assert build.provider == "targon"
        assert build.status == "fallback_required"
        assert build.error_code == "TARGON_PROVISION_TIMEOUT"


@pytest.mark.asyncio
async def test_targon_inflight_cap_holds_eleventh_without_fallback(
    session_maker: async_sessionmaker[AsyncSession],
) -> None:
    await _seed_agent(
        session_maker, status=AgentStatus.UPLOADED, name="one", sha256="aa" * 32
    )
    await _seed_agent(
        session_maker, status=AgentStatus.UPLOADED, name="two", sha256="bb" * 32
    )
    targon = _FakeTargon()
    loop = TargonRentalLoop(
        session_maker=session_maker,
        config=_config(max_inflight=1),
        targon=targon,
        screener_hotkey=_SCREENER_HOTKEY,
        interval_seconds=60,
    )
    assert await loop.tick() is True
    await loop.tick()
    assert len(targon.created) == 1
    async with session_maker() as session:
        builds = (await session.scalars(select(SubmissionImageBuild))).all()
        statuses = sorted(build.status for build in builds)
        assert statuses == ["queued", "running"]


@pytest.mark.asyncio
async def test_targon_inflight_cap_does_not_overflow_to_cloudrun(
    session_maker: async_sessionmaker[AsyncSession],
) -> None:
    await _seed_agent(
        session_maker, status=AgentStatus.UPLOADED, name="one", sha256="aa" * 32
    )
    await _seed_agent(
        session_maker, status=AgentStatus.UPLOADED, name="two", sha256="bb" * 32
    )
    targon = _FakeTargon()
    cloudrun = _FakeCloudRun()
    config = _config(max_inflight=1)
    loop = TargonRentalLoop(
        session_maker=session_maker,
        config=config,
        targon=targon,
        screener_hotkey=_SCREENER_HOTKEY,
        providers=[TargonComputeProvider(targon, config), cloudrun],
        interval_seconds=60,
    )
    await loop.tick()
    await loop.tick()
    assert len(targon.created) == 1
    assert cloudrun.builds == []
    async with session_maker() as session:
        builds = (await session.scalars(select(SubmissionImageBuild))).all()
        statuses = sorted(build.status for build in builds)
        assert statuses == ["queued", "running"]


@pytest.mark.asyncio
async def test_tick_finalizes_running_attempt_after_smoke(
    session_maker: async_sessionmaker[AsyncSession],
) -> None:
    await _seed_agent(session_maker, status=AgentStatus.UPLOADED)
    targon = _FakeTargon()
    called: list[UUID] = []

    async def complete_screen(attempt_id: UUID) -> None:
        called.append(attempt_id)

    loop = TargonRentalLoop(
        session_maker=session_maker,
        config=_config(),
        targon=targon,
        screener_hotkey=_SCREENER_HOTKEY,
        complete_screen=complete_screen,
        interval_seconds=60,
    )
    await loop.tick()
    async with session_maker() as session, session.begin():
        build = await session.scalar(select(SubmissionImageBuild).limit(1))
        assert build is not None
        build.status = "succeeded"
        build.runtime_status = "succeeded"
        build.output_sha256 = "12" * 32
        build.output_size_bytes = 123
        attempt_id = build.attempt_id
    await loop.tick()
    assert called == [attempt_id]


@pytest.mark.asyncio
async def test_reaper_parks_dead_targon_kaniko_without_cloudrun(
    session_maker: async_sessionmaker[AsyncSession],
) -> None:
    await _seed_agent(session_maker, status=AgentStatus.UPLOADED)
    targon = _FakeTargon()
    cloudrun = _FakeCloudRun()
    traces: list[tuple[str, bytes, str]] = []

    async def traces_put(key: str, body: bytes, content_type: str) -> str:
        traces.append((key, body, content_type))
        return key

    config = _config()
    loop = TargonRentalLoop(
        session_maker=session_maker,
        config=config,
        targon=targon,
        screener_hotkey=_SCREENER_HOTKEY,
        providers=[TargonComputeProvider(targon, config), cloudrun],
        traces_put=traces_put,
        interval_seconds=60,
    )
    assert await loop.tick() is True
    async with session_maker() as session:
        build = await session.scalar(select(SubmissionImageBuild).limit(1))
        assert build is not None
        assert build.provider == "targon"
        assert build.status == "running"
    targon.status = "error"
    targon.message = "Container failed (Error) — exit code 1"
    assert await loop.tick() is True
    assert targon.deleted == ["wrk-1"]
    assert cloudrun.builds == []
    assert traces
    assert traces[0][0].startswith("traces/v1/lane=screening/kind=kaniko/")
    async with session_maker() as session:
        build = await session.scalar(select(SubmissionImageBuild).limit(1))
        assert build is not None
        assert build.provider == "targon"
        assert build.status == "fallback_required"
        assert build.error_code == "TARGON_PROVISION_ERROR"
        attempt = await session.get(ScreeningAttempt, build.attempt_id)
        assert attempt is not None
        assert attempt.failure_provider == "targon"
        assert attempt.failure_lane == "kaniko"
        assert "exit code 1" in (attempt.private_failure_detail or "")
        assert attempt.private_failure_log_tail == "kaniko: rustc oom"
        assert attempt.failure_captured_at is not None


@pytest.mark.asyncio
async def test_kaniko_exit_72_does_not_requeue_to_cloudrun(
    session_maker: async_sessionmaker[AsyncSession],
) -> None:
    await _seed_agent(session_maker, status=AgentStatus.UPLOADED)
    targon = _FakeTargon()
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
    await loop.tick()
    targon.status = "error"
    targon.message = "Container failed (Error) — exit code 72"
    targon.deleted.clear()
    assert await loop.tick() is True
    assert "wrk-1" in targon.deleted
    assert cloudrun.builds == []
    async with session_maker() as session:
        build = await session.scalar(select(SubmissionImageBuild).limit(1))
        assert build is not None
        assert build.status == "fallback_required"
        assert build.error_code == "TARGON_SUBMISSION_KANIKO_FAILED"


@pytest.mark.asyncio
async def test_prior_gcp_infra_failure_stays_parked_without_manual_retry(
    session_maker: async_sessionmaker[AsyncSession],
) -> None:
    agent_id = await _seed_agent(session_maker, status=AgentStatus.SCREENING_FAILED)
    prior_attempt_id = uuid4()
    prior_build_id = uuid4()
    now = datetime.now(UTC)
    async with session_maker() as session, session.begin():
        session.add(
            ScreeningAttempt(
                attempt_id=prior_attempt_id,
                agent_id=agent_id,
                screener_hotkey=_SCREENER_HOTKEY,
                policy_version=SCREENING_POLICY_VERSION,
                status="failed",
                started_at=now - timedelta(hours=2),
                deadline=now - timedelta(minutes=50),
                finished_at=now - timedelta(minutes=50),
                reason_code="cloudrun-build-unavailable",
                build_only=True,
            )
        )
        session.add(
            SubmissionImageBuild(
                build_id=prior_build_id,
                agent_id=agent_id,
                attempt_id=prior_attempt_id,
                environment="prod",
                artifact_sha256=_SHA256,
                image_ref=f"ditto-screen/{agent_id}-{prior_attempt_id}:latest",
                output_key=f"remote-builds/{prior_build_id}/image.tar",
                status="fallback_required",
                provider="gcp",
                error_code="CLOUDRUN_PROVISION_ERROR",
                runtime_status="skipped",
                attempt_count=2,
            )
        )
    targon = _FakeTargon()
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
    assert await loop.tick() is False
    async with session_maker() as session:
        current = await session.scalar(
            select(SubmissionImageBuild)
            .where(SubmissionImageBuild.build_id != prior_build_id)
            .limit(1)
        )
        assert current is None
    assert targon.created == []
    assert cloudrun.builds == []


@pytest.mark.asyncio
async def test_kaniko_queued_row_keeps_selected_targon_provider(
    session_maker: async_sessionmaker[AsyncSession],
) -> None:
    await _seed_agent(session_maker, status=AgentStatus.UPLOADED)
    targon = _FakeTargon()
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
    async with session_maker() as session, session.begin():
        build = await session.scalar(select(SubmissionImageBuild).limit(1))
        assert build is not None
        build.status = "queued"
        build.error_code = "TARGON_PROVISION_ERROR"
        build.provider = None
        build.provider_resource_id = None
    assert await loop.tick() is True
    assert len(targon.created) == 2
    assert cloudrun.builds == []
    async with session_maker() as session:
        build = await session.scalar(select(SubmissionImageBuild).limit(1))
        assert build is not None
        assert build.provider == "targon"


@pytest.mark.asyncio
async def test_tick_finalizes_fallback_required_build(
    session_maker: async_sessionmaker[AsyncSession],
) -> None:
    await _seed_agent(session_maker, status=AgentStatus.UPLOADED)
    targon = _FakeTargon()
    called: list[UUID] = []

    async def complete_screen(attempt_id: UUID) -> None:
        called.append(attempt_id)

    loop = TargonRentalLoop(
        session_maker=session_maker,
        config=_config(),
        targon=targon,
        screener_hotkey=_SCREENER_HOTKEY,
        complete_screen=complete_screen,
        interval_seconds=60,
    )
    await loop.tick()
    async with session_maker() as session, session.begin():
        build = await session.scalar(select(SubmissionImageBuild).limit(1))
        assert build is not None
        build.status = "fallback_required"
        build.error_code = "CLOUDRUN_PROVISION_TIMEOUT"
        attempt_id = build.attempt_id
    await loop.tick()
    assert called == [attempt_id]


@pytest.mark.asyncio
async def test_tick_finalizes_succeeded_build_after_runtime_fallback(
    session_maker: async_sessionmaker[AsyncSession],
) -> None:
    await _seed_agent(session_maker, status=AgentStatus.UPLOADED)
    targon = _FakeTargon()
    called: list[UUID] = []

    async def complete_screen(attempt_id: UUID) -> None:
        called.append(attempt_id)

    loop = TargonRentalLoop(
        session_maker=session_maker,
        config=_config(),
        targon=targon,
        screener_hotkey=_SCREENER_HOTKEY,
        complete_screen=complete_screen,
        interval_seconds=60,
    )
    await loop.tick()
    async with session_maker() as session, session.begin():
        build = await session.scalar(select(SubmissionImageBuild).limit(1))
        assert build is not None
        build.status = "succeeded"
        build.runtime_status = "fallback_required"
        build.runtime_error_code = "TARGON_PROVISION_TIMEOUT"
        build.output_sha256 = "12" * 32
        build.output_size_bytes = 123
        attempt_id = build.attempt_id
    await loop.tick()
    assert called == [attempt_id]


@pytest.mark.asyncio
async def test_runtime_cloudrun_provision_failure_stays_parked(
    session_maker: async_sessionmaker[AsyncSession],
) -> None:
    agent_id = await _seed_agent(session_maker, status=AgentStatus.SCREENING_FAILED)
    prior_attempt_id = uuid4()
    prior_build_id = uuid4()
    now = datetime.now(UTC)
    archive_key = f"remote-builds/{prior_build_id}/image.tar"
    async with session_maker() as session, session.begin():
        session.add(
            ScreeningAttempt(
                attempt_id=prior_attempt_id,
                agent_id=agent_id,
                screener_hotkey=_SCREENER_HOTKEY,
                policy_version=SCREENING_POLICY_VERSION,
                status="failed",
                started_at=now - timedelta(hours=2),
                deadline=now - timedelta(minutes=50),
                finished_at=now - timedelta(minutes=50),
                reason_code="cloudrun-runtime-unavailable",
                build_only=True,
            )
        )
        session.add(
            SubmissionImageBuild(
                build_id=prior_build_id,
                agent_id=agent_id,
                attempt_id=prior_attempt_id,
                environment="prod",
                artifact_sha256=_SHA256,
                image_ref=f"ditto-screen/{agent_id}-{prior_attempt_id}:latest",
                output_key=archive_key,
                status="succeeded",
                provider="targon",
                output_sha256="12" * 32,
                output_size_bytes=123,
                output_image_id="sha256:" + "ab" * 32,
                runtime_status="fallback_required",
                runtime_error_code="CLOUDRUN_PROVISION_ERROR",
                attempt_count=1,
                completed_at=now - timedelta(minutes=50),
            )
        )
    targon = _FakeTargon()
    cloudrun = _FakeCloudRun()
    promoted: list[str] = []

    async def promote(key: str, destination: str, _writer: str) -> str:
        del destination
        promoted.append(key)
        return (
            "us-central1-docker.pkg.dev/ditto-app-dev/"
            "ditto-screening-candidates/miner@sha256:" + "cd" * 32
        )

    async def mint(_sa: str) -> str:
        return "token-" + "x" * 120

    config = _config()
    loop = TargonRentalLoop(
        session_maker=session_maker,
        config=config,
        targon=targon,
        screener_hotkey=_SCREENER_HOTKEY,
        promote_archive=promote,
        mint_token=mint,
        providers=[TargonComputeProvider(targon, config), cloudrun],
        interval_seconds=60,
    )
    assert await loop.tick() is False
    async with session_maker() as session:
        current = await session.scalar(
            select(SubmissionImageBuild)
            .where(SubmissionImageBuild.build_id != prior_build_id)
            .limit(1)
        )
        assert current is None
    assert cloudrun.builds == []
    assert promoted == []


@pytest.mark.asyncio
async def test_review_failure_reuses_verified_build_and_successful_smoke(
    session_maker: async_sessionmaker[AsyncSession],
) -> None:
    agent_id = await _seed_agent(session_maker, status=AgentStatus.UPLOADED)
    prior_attempt_id = uuid4()
    prior_build_id = uuid4()
    now = datetime.now(UTC)
    archive_key = f"remote-builds/{prior_build_id}/image.tar"
    runtime_image = (
        "us-central1-docker.pkg.dev/ditto-app-dev/"
        "ditto-screening-candidates/miner@sha256:" + "cd" * 32
    )
    async with session_maker() as session, session.begin():
        session.add(
            ScreeningAttempt(
                attempt_id=prior_attempt_id,
                agent_id=agent_id,
                screener_hotkey=_SCREENER_HOTKEY,
                policy_version=SCREENING_POLICY_VERSION,
                status="failed",
                started_at=now - timedelta(hours=2),
                deadline=now - timedelta(minutes=50),
                finished_at=now - timedelta(minutes=50),
                reason_code="source-review-retryable-infra",
                build_only=False,
            )
        )
        session.add(
            SubmissionImageBuild(
                build_id=prior_build_id,
                agent_id=agent_id,
                attempt_id=prior_attempt_id,
                environment="prod",
                artifact_sha256=_SHA256,
                image_ref=f"ditto-screen/{agent_id}-{prior_attempt_id}:latest",
                output_key=archive_key,
                status="succeeded",
                provider="targon",
                output_sha256="12" * 32,
                output_size_bytes=123,
                output_image_id="sha256:" + "ab" * 32,
                runtime_status="succeeded",
                runtime_image_reference=runtime_image,
                runtime_completed_at=now - timedelta(minutes=55),
                attempt_count=1,
                completed_at=now - timedelta(minutes=56),
            )
        )

    current_attempt_id = uuid4()
    async with session_maker() as session, session.begin():
        agent = await session.get(Agent, agent_id)
        assert agent is not None
        current_attempt = ScreeningAttempt(
            attempt_id=current_attempt_id,
            agent_id=agent_id,
            screener_hotkey=_SCREENER_HOTKEY,
            policy_version=SCREENING_POLICY_VERSION,
            status="running",
            started_at=now,
            deadline=now + timedelta(hours=2),
            build_only=False,
        )
        session.add(current_attempt)
        await session.flush()
        await _queue_kaniko(
            session,
            agent=agent,
            attempt=current_attempt,
            environment="prod",
            runtime_enabled=True,
        )

    async with session_maker() as session:
        current = await session.scalar(
            select(SubmissionImageBuild)
            .where(SubmissionImageBuild.attempt_id == current_attempt_id)
            .limit(1)
        )
        assert current is not None
        assert current.status == "succeeded"
        assert current.output_key == archive_key
        assert current.output_sha256 == "12" * 32
        assert current.output_image_id == "sha256:" + "ab" * 32
        assert current.runtime_status == "succeeded"
        assert current.runtime_image_reference == runtime_image
        assert current.runtime_completed_at is not None


@pytest.mark.asyncio
async def test_missing_archive_is_not_recycled(
    session_maker: async_sessionmaker[AsyncSession],
) -> None:
    agent_id = await _seed_agent(session_maker, status=AgentStatus.UPLOADED)
    prior_attempt_id = uuid4()
    current_attempt_id = uuid4()
    prior_build_id = uuid4()
    now = datetime.now(UTC)
    stale_key = f"remote-builds/{prior_build_id}/image.tar"

    async with session_maker() as session, session.begin():
        session.add_all(
            [
                ScreeningAttempt(
                    attempt_id=prior_attempt_id,
                    agent_id=agent_id,
                    screener_hotkey=_SCREENER_HOTKEY,
                    policy_version=SCREENING_POLICY_VERSION,
                    status="failed",
                    started_at=now - timedelta(hours=2),
                    deadline=now - timedelta(hours=1),
                    finished_at=now - timedelta(hours=1),
                    build_only=True,
                ),
                SubmissionImageBuild(
                    build_id=prior_build_id,
                    agent_id=agent_id,
                    attempt_id=prior_attempt_id,
                    environment="prod",
                    artifact_sha256=_SHA256,
                    image_ref=(f"ditto-screen/{agent_id}-{prior_attempt_id}:latest"),
                    output_key=stale_key,
                    status="succeeded",
                    provider="targon",
                    output_sha256="12" * 32,
                    output_size_bytes=123,
                    output_image_id="sha256:" + "ab" * 32,
                    runtime_status="skipped",
                    completed_at=now - timedelta(hours=1),
                ),
            ]
        )
        agent = await session.get(Agent, agent_id)
        assert agent is not None
        current_attempt = ScreeningAttempt(
            attempt_id=current_attempt_id,
            agent_id=agent_id,
            screener_hotkey=_SCREENER_HOTKEY,
            policy_version=SCREENING_POLICY_VERSION,
            status="running",
            started_at=now,
            deadline=now + _LEASE_TTL,
            build_only=True,
        )
        session.add(current_attempt)
        await session.flush()

        async def missing(*, key: str) -> bool:
            assert key == stale_key
            return False

        await _queue_kaniko(
            session,
            agent=agent,
            attempt=current_attempt,
            environment="prod",
            runtime_enabled=False,
            archive_exists=missing,
        )

    async with session_maker() as session:
        current = await session.scalar(
            select(SubmissionImageBuild).where(
                SubmissionImageBuild.attempt_id == current_attempt_id
            )
        )
        assert current is not None
        assert current.status == "queued"
        assert current.output_key != stale_key


@pytest.mark.asyncio
async def test_kaniko_uses_resolved_builder_image(
    session_maker: async_sessionmaker[AsyncSession],
) -> None:
    await _seed_agent(session_maker, status=AgentStatus.UPLOADED)
    targon = _FakeTargon()
    resolved = (
        "us-central1-docker.pkg.dev/ditto-app-dev/"
        "ditto-public-builders/submission-builder@sha256:" + "cd" * 32
    )
    loop = TargonRentalLoop(
        session_maker=session_maker,
        config=_config(),
        targon=targon,
        screener_hotkey=_SCREENER_HOTKEY,
        resolve_builder_image=lambda _image: resolved,
        interval_seconds=60,
    )
    await loop.tick()
    assert targon.created
    assert targon.created[0]["image"] == resolved


@pytest.mark.asyncio
async def test_kaniko_reresolves_builder_image_each_launch(
    session_maker: async_sessionmaker[AsyncSession],
) -> None:
    await _seed_agent(session_maker, status=AgentStatus.UPLOADED)
    images = [
        "us-central1-docker.pkg.dev/ditto-app-dev/"
        "ditto-public-builders/submission-builder@sha256:" + "aa" * 32,
        "us-central1-docker.pkg.dev/ditto-app-dev/"
        "ditto-public-builders/submission-builder@sha256:" + "bb" * 32,
    ]

    loop = TargonRentalLoop(
        session_maker=session_maker,
        config=_config(),
        targon=_FakeTargon(),
        screener_hotkey=_SCREENER_HOTKEY,
        resolve_builder_image=lambda _image: images.pop(0),
        interval_seconds=60,
    )
    assert loop._builder_image().endswith("aa" * 32)
    assert loop._builder_image().endswith("bb" * 32)


@pytest.mark.asyncio
async def test_kaniko_does_not_launch_unpinned_builder_image(
    session_maker: async_sessionmaker[AsyncSession],
) -> None:
    await _seed_agent(session_maker, status=AgentStatus.UPLOADED)
    targon = _FakeTargon()
    loop = TargonRentalLoop(
        session_maker=session_maker,
        config=_config(
            submission_builder_image=(
                "us-central1-docker.pkg.dev/ditto-app-dev/"
                "ditto-public-builders/submission-builder:sha-" + "ab" * 20
            )
        ),
        targon=targon,
        screener_hotkey=_SCREENER_HOTKEY,
        resolve_builder_image=lambda image: image,
        interval_seconds=60,
    )
    await loop.tick()
    assert targon.created == []
    async with session_maker() as session:
        build = await session.scalar(select(SubmissionImageBuild).limit(1))
        assert build is not None
        assert build.status == "queued"
