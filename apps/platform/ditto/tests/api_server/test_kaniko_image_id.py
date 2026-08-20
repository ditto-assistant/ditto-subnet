from __future__ import annotations

from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4

import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from ditto.api_models.agent_status import AgentStatus
from ditto.api_models.screener import SCREENING_POLICY_VERSION
from ditto.api_server.targon_screening import repair_kaniko_screened_image_identities
from ditto.db.models import (
    Agent,
    ScreenedImageUpload,
    ScreeningAttempt,
    SubmissionImageBuild,
)
from ditto.tests.api_server.endpoints.test_screener import (
    _SCREENER_HOTKEY,
    _SHA256,
    _seed_agent,
)

_AR_DIGEST = "sha256:" + "cd" * 32
_CONFIG_DIGEST = "sha256:" + "ab" * 32
_RUNTIME_REF = (
    "us-central1-docker.pkg.dev/ditto-app-dev/"
    "ditto-screening-candidates/miner@" + _AR_DIGEST
)


async def _seed_evaluating_kaniko(
    maker: async_sessionmaker[AsyncSession],
    *,
    screened_image_id: str,
    runtime_image_reference: str | None = _RUNTIME_REF,
) -> tuple[UUID, UUID]:
    agent_id = await _seed_agent(maker, status=AgentStatus.EVALUATING)
    attempt_id = uuid4()
    image_upload_id = uuid4()
    now = datetime.now(UTC)
    async with maker() as session, session.begin():
        session.add(
            ScreeningAttempt(
                attempt_id=attempt_id,
                agent_id=agent_id,
                screener_hotkey=_SCREENER_HOTKEY,
                policy_version=SCREENING_POLICY_VERSION,
                status="passed",
                started_at=now - timedelta(minutes=10),
                deadline=now + timedelta(minutes=60),
                finished_at=now - timedelta(minutes=1),
            )
        )
        await session.flush()
        session.add(
            ScreenedImageUpload(
                image_upload_id=image_upload_id,
                agent_id=agent_id,
                attempt_id=attempt_id,
                screener_hotkey=_SCREENER_HOTKEY,
                storage_upload_id=f"storage-{image_upload_id}",
                sha256="12" * 32,
                size_bytes=123,
                image_id=screened_image_id,
                image_ref=f"ditto-screen/{agent_id}:latest",
                status="verified",
                expires_at=now + timedelta(hours=1),
                verified_at=now,
            )
        )
        session.add(
            SubmissionImageBuild(
                build_id=uuid4(),
                agent_id=agent_id,
                attempt_id=attempt_id,
                environment="prod",
                artifact_sha256=_SHA256,
                image_ref=f"ditto-screen/{agent_id}-{attempt_id}:latest",
                output_key=f"{agent_id}/builds/{attempt_id}.tar",
                status="consumed",
                provider="targon",
                output_sha256="12" * 32,
                output_size_bytes=123,
                runtime_status="succeeded",
                runtime_image_reference=runtime_image_reference,
                attempt_count=1,
                created_at=now - timedelta(minutes=10),
                started_at=now - timedelta(minutes=9),
                completed_at=now - timedelta(minutes=2),
                consumed_at=now - timedelta(minutes=1),
                updated_at=now - timedelta(minutes=1),
            )
        )
        agent = await session.get(Agent, agent_id)
        assert agent is not None
        agent.screened_image_sha256 = "12" * 32
        agent.screened_image_size_bytes = 123
        agent.screened_image_id = screened_image_id
        agent.screened_image_ref = f"ditto-screen/{agent_id}:latest"
        agent.screened_image_upload_id = image_upload_id
        agent.screened_image_verified_at = now
    return agent_id, image_upload_id


@pytest.mark.asyncio
async def test_repair_repins_artifact_registry_digest_to_config_digest(
    session_maker: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    agent_id, _upload_id = await _seed_evaluating_kaniko(
        session_maker, screened_image_id=_AR_DIGEST
    )

    async def _inspect(ref: str | None) -> str | None:
        assert ref == _RUNTIME_REF
        return _CONFIG_DIGEST

    monkeypatch.setattr(
        "ditto.api_server.targon_screening.config_digest_from_runtime_image",
        _inspect,
    )

    repaired = await repair_kaniko_screened_image_identities(session_maker)

    assert repaired == 1
    async with session_maker() as session:
        agent = await session.get(Agent, agent_id)
        assert agent is not None
        assert agent.screened_image_id == _CONFIG_DIGEST
        upload = await session.get(ScreenedImageUpload, agent.screened_image_upload_id)
        assert upload is not None
        assert upload.image_id == _CONFIG_DIGEST


@pytest.mark.asyncio
async def test_repair_skips_already_pinned_config_digest(
    session_maker: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    await _seed_evaluating_kaniko(session_maker, screened_image_id=_CONFIG_DIGEST)
    calls: list[str] = []

    async def _inspect(ref: str | None) -> str | None:
        calls.append(ref or "")
        return _CONFIG_DIGEST

    monkeypatch.setattr(
        "ditto.api_server.targon_screening.config_digest_from_runtime_image",
        _inspect,
    )

    repaired = await repair_kaniko_screened_image_identities(session_maker)

    assert repaired == 0
    assert calls == []
