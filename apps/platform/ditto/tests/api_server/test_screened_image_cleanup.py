"""Retention tests for eligibility-aware screened-image cleanup."""

from __future__ import annotations

import logging
import os
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
    CleanupResult,
    PreservationConfigError,
    cleanup_screened_images,
    emergency_preservation_enabled,
    screened_image_key,
)
from ditto.api_server.storage import ListedObject, MultipartUpload
from ditto.db.models import Agent


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
    # The M0 breaker fails safe: cleanup is disabled unless explicitly enabled.
    # This test exercises the destructive path, so it must opt in.
    monkeypatch.setenv("M0_EMERGENCY_PRESERVATION_MODE", "false")
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


def _exploding_storage() -> MagicMock:
    """Storage whose every destructive dependency fails if touched at all.

    Stronger than asserting zero deletions: it proves the breaker returns before
    any listing, query or delete is even attempted.
    """
    storage = MagicMock()
    for name in (
        "list_multipart_uploads",
        "abort_multipart_upload",
        "list_objects",
        "delete_object",
    ):
        setattr(
            storage,
            name,
            AsyncMock(
                side_effect=AssertionError(
                    f"storage.{name} must not be touched while the M0 "
                    "preservation breaker is active"
                )
            ),
        )
    return storage


def _exploding_session_maker() -> MagicMock:
    maker = MagicMock(
        side_effect=AssertionError(
            "the database must not be touched while the M0 preservation "
            "breaker is active"
        )
    )
    return maker


@pytest.mark.parametrize(
    "flag",
    ["true", "TRUE", " on ", "1", "yes"],
)
async def test_breaker_disables_cleanup_when_flag_enabled(
    monkeypatch: pytest.MonkeyPatch, flag: str, caplog: pytest.LogCaptureFixture
) -> None:
    monkeypatch.setenv("M0_EMERGENCY_PRESERVATION_MODE", flag)

    with caplog.at_level(logging.WARNING):
        result = await cleanup_screened_images(
            _exploding_session_maker(), _exploding_storage()
        )

    assert result == CleanupResult.preserved()
    assert result.preservation_mode is True
    assert result.aborted_multipart == 0
    assert result.deleted_superseded == 0
    assert result.deleted_orphans == 0
    assert "M0 emergency preservation mode active" in caplog.text


async def test_breaker_defaults_to_disabled_when_flag_absent(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    monkeypatch.delenv("M0_EMERGENCY_PRESERVATION_MODE", raising=False)

    with caplog.at_level(logging.WARNING):
        result = await cleanup_screened_images(
            _exploding_session_maker(), _exploding_storage()
        )

    assert result.preservation_mode is True
    assert "M0 emergency preservation mode active" in caplog.text


@pytest.mark.parametrize("flag", ["maybe", "", "  ", "2", "disabled"])
async def test_breaker_fails_safe_on_malformed_flag(
    monkeypatch: pytest.MonkeyPatch, flag: str, caplog: pytest.LogCaptureFixture
) -> None:
    monkeypatch.setenv("M0_EMERGENCY_PRESERVATION_MODE", flag)

    with caplog.at_level(logging.ERROR):
        result = await cleanup_screened_images(
            _exploding_session_maker(), _exploding_storage()
        )

    assert result.preservation_mode is True
    assert "configuration is invalid" in caplog.text
    assert "DISABLED (fail-safe)" in caplog.text


@pytest.mark.parametrize("flag", ["false", "FALSE", " off ", "0", "no"])
def test_parser_accepts_explicit_disable(
    monkeypatch: pytest.MonkeyPatch, flag: str
) -> None:
    monkeypatch.setenv("M0_EMERGENCY_PRESERVATION_MODE", flag)
    assert emergency_preservation_enabled() is False


def test_parser_rejects_plain_truthiness(monkeypatch: pytest.MonkeyPatch) -> None:
    """bool("false") is True — the mistake this parser exists to prevent."""
    monkeypatch.setenv("M0_EMERGENCY_PRESERVATION_MODE", "false")
    assert bool(os.environ["M0_EMERGENCY_PRESERVATION_MODE"]) is True
    assert emergency_preservation_enabled() is False


def test_parser_raises_on_unrecognised_value(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("M0_EMERGENCY_PRESERVATION_MODE", "maybe")
    with pytest.raises(PreservationConfigError):
        emergency_preservation_enabled()


async def test_pm2_entry_path_cannot_bypass_the_breaker(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """The deployed pm2 process calls this module; it must hit the same guard."""
    import importlib.util
    from pathlib import Path

    entry = (
        Path(__file__).resolve().parents[3] / "scripts" / "cleanup_screened_images.py"
    )
    assert entry.exists(), entry
    spec = importlib.util.spec_from_file_location("m0_entry_probe", entry)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)

    assert module.cleanup_screened_images is cleanup_screened_images

    monkeypatch.setenv("M0_EMERGENCY_PRESERVATION_MODE", "true")
    with caplog.at_level(logging.WARNING):
        result = await module.cleanup_screened_images(
            _exploding_session_maker(), _exploding_storage()
        )
    assert result.preservation_mode is True
