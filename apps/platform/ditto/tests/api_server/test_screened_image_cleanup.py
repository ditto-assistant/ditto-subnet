"""Retention tests for eligibility-aware screened-image cleanup."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock
from uuid import uuid4

import pytest
from sqlalchemy.ext.asyncio import (
    AsyncSession,
    async_sessionmaker,
)

from ditto.api_models.agent_status import AgentStatus
from ditto.api_server.screened_image_cleanup import (
    cleanup_screened_images,
    screened_image_key,
)
from ditto.api_server.storage import ListedObject, MultipartUpload
from ditto.db.models import (
    Agent,
    ScreenedImageUpload,
    ScreeningAttempt,
    ScreeningQuarantine,
)


def _agent(*, status: AgentStatus, created_at: datetime) -> Agent:
    agent_id = uuid4()
    upload_id = uuid4()
    return Agent(
        agent_id=agent_id,
        miner_hotkey=f"miner-{agent_id}",
        name=f"agent-{agent_id}",
        version=1,
        sha256="11" * 32,
        size_bytes=1,
        status=status,
        created_at=created_at,
        screened_image_sha256="22" * 32,
        screened_image_size_bytes=123,
        screened_image_id="sha256:" + "33" * 32,
        screened_image_ref=f"ditto-screen/{agent_id}:latest",
        screened_image_upload_id=upload_id,
        screened_image_verified_at=created_at,
    )


async def test_cleanup_preserves_active_and_removes_superseded_and_abandoned(
    session_maker: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    now = datetime.now(UTC)
    active = _agent(status=AgentStatus.EVALUATING, created_at=now - timedelta(days=60))
    superseded = _agent(
        status=AgentStatus.REJECTED, created_at=now - timedelta(days=60)
    )
    champion = _agent(status=AgentStatus.SCORED, created_at=now - timedelta(days=60))
    async with session_maker() as session, session.begin():
        session.add_all([active, superseded, champion])

    monkeypatch.setattr(
        "ditto.api_server.screened_image_cleanup.list_eligible_ledger",
        AsyncMock(return_value=[SimpleNamespace(agent_id=champion.agent_id)]),
    )

    active_key = screened_image_key(active.agent_id, active.screened_image_upload_id)
    superseded_key = screened_image_key(
        superseded.agent_id, superseded.screened_image_upload_id
    )
    champion_key = screened_image_key(
        champion.agent_id, champion.screened_image_upload_id
    )
    orphan_key = f"{uuid4()}/screened-images/{uuid4()}.tar"
    storage = MagicMock()
    storage.list_multipart_uploads = AsyncMock(
        return_value=[
            MultipartUpload(
                key=f"{uuid4()}/screened-images/{uuid4()}.tar",
                upload_id="stale-upload",
                initiated_at=now - timedelta(days=2),
            )
        ]
    )
    storage.abort_multipart_upload = AsyncMock()
    storage.list_objects = AsyncMock(
        return_value=[
            ListedObject(key=active_key, last_modified=now - timedelta(days=60)),
            ListedObject(key=champion_key, last_modified=now - timedelta(days=60)),
            ListedObject(key=superseded_key, last_modified=now - timedelta(days=60)),
            ListedObject(key=orphan_key, last_modified=now - timedelta(days=2)),
        ]
    )
    storage.delete_object = AsyncMock()

    result = await cleanup_screened_images(session_maker, storage, now=now)

    assert result.aborted_multipart == 1
    assert result.deleted_superseded == 1
    assert result.deleted_orphans == 1
    deleted = [call.kwargs["key"] for call in storage.delete_object.await_args_list]
    assert superseded_key in deleted
    assert orphan_key in deleted
    assert active_key not in deleted
    assert champion_key not in deleted
    async with session_maker() as session:
        kept = await session.get(Agent, active.agent_id)
        kept_champion = await session.get(Agent, champion.agent_id)
        cleared = await session.get(Agent, superseded.agent_id)
        assert kept is not None and kept.screened_image_upload_id is not None
        assert (
            kept_champion is not None
            and kept_champion.screened_image_upload_id is not None
        )
        assert cleared is not None and cleared.screened_image_upload_id is None


async def test_cleanup_retains_verified_image_for_active_source_hold(
    session_maker: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    now = datetime.now(UTC)
    old = now - timedelta(days=2)
    held = _agent(status=AgentStatus.QUARANTINED, created_at=old)
    held.screened_image_sha256 = None
    held.screened_image_size_bytes = None
    held.screened_image_id = None
    held.screened_image_ref = None
    held.screened_image_upload_id = None
    held.screened_image_verified_at = None
    attempt_id = uuid4()
    upload_id = uuid4()
    hotkey = "screener-held-image"
    async with session_maker() as session, session.begin():
        session.add(held)
        await session.flush()
        session.add(
            ScreeningAttempt(
                attempt_id=attempt_id,
                agent_id=held.agent_id,
                artifact_sha256=held.sha256,
                screener_hotkey=hotkey,
                policy_version=13,
                status="quarantined",
                started_at=old,
                deadline=old + timedelta(minutes=15),
                finished_at=old + timedelta(minutes=10),
            )
        )
        await session.flush()
        session.add(
            ScreeningQuarantine(
                quarantine_id=uuid4(),
                agent_id=held.agent_id,
                attempt_id=attempt_id,
                screener_hotkey=hotkey,
                policy_version=13,
                manifest_digest="ab" * 32,
                reason_code="adjudicated-source-review-escalate",
                evidence=[],
                status="active",
                created_at=old,
            )
        )
        session.add(
            ScreenedImageUpload(
                image_upload_id=upload_id,
                agent_id=held.agent_id,
                attempt_id=attempt_id,
                screener_hotkey=hotkey,
                storage_upload_id="verified-held-image",
                sha256="22" * 32,
                size_bytes=123,
                image_id="sha256:" + "33" * 32,
                image_ref=f"ditto-screen/{held.agent_id}:latest",
                status="verified",
                created_at=old,
                expires_at=old + timedelta(minutes=15),
                verified_at=old + timedelta(minutes=5),
            )
        )

    monkeypatch.setattr(
        "ditto.api_server.screened_image_cleanup.list_eligible_ledger",
        AsyncMock(return_value=[]),
    )
    held_key = screened_image_key(held.agent_id, upload_id)
    orphan_key = f"{uuid4()}/screened-images/{uuid4()}.tar"
    storage = MagicMock()
    storage.list_multipart_uploads = AsyncMock(return_value=[])
    storage.list_objects = AsyncMock(
        return_value=[
            ListedObject(key=held_key, last_modified=old),
            ListedObject(key=orphan_key, last_modified=old),
        ]
    )
    storage.delete_object = AsyncMock()

    result = await cleanup_screened_images(session_maker, storage, now=now)

    assert result.deleted_orphans == 1
    storage.delete_object.assert_awaited_once_with(key=orphan_key)
