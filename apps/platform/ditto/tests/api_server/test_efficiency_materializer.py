from __future__ import annotations

import asyncio
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from typing import cast
from unittest.mock import AsyncMock

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from ditto.api_server import efficiency
from ditto.api_server.config import EfficiencyBonusConfig
from ditto.api_server.efficiency import (
    EfficiencyEvidenceWatermark,
    EfficiencyStateMaterializer,
)


class _Transaction:
    async def __aenter__(self) -> None:
        return None

    async def __aexit__(self, *_args: object) -> None:
        return None


class _Session:
    def begin(self) -> _Transaction:
        return _Transaction()


def _session() -> AsyncSession:
    return cast(AsyncSession, _Session())


_NOW = datetime(2026, 8, 15, tzinfo=UTC)
_ENABLED = EfficiencyBonusConfig(enabled=True, fold_enabled=True)
_WATERMARK = EfficiencyEvidenceWatermark(
    active_bench_version=9,
    candidate_digest="candidates",
    owner_attestation_digest="owners",
    score_count=339,
    score_updated_at=_NOW,
    confirmation_score_count=15,
    confirmation_score_created_at=_NOW,
    confirmation_subject_count=123,
    confirmation_subject_updated_at=_NOW,
    confirmation_bundle_count=44,
    confirmation_bundle_updated_at=_NOW,
    confirmation_settings_revision=3,
)


@pytest.mark.asyncio
async def test_unchanged_watermark_skips_expensive_materialization(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    load = AsyncMock(return_value=_WATERMARK)
    materialize = AsyncMock()
    monkeypatch.setattr(efficiency, "_efficiency_evidence_watermark", load)
    monkeypatch.setattr(efficiency, "ensure_efficiency_state", materialize)
    coordinator = EfficiencyStateMaterializer(poll_seconds=0)
    session = _session()

    await coordinator.ensure(session, _ENABLED, now=_NOW)
    await coordinator.ensure(session, _ENABLED, now=_NOW)

    assert load.await_count == 2
    materialize.assert_awaited_once_with(session, _ENABLED, now=_NOW)


@pytest.mark.asyncio
async def test_evidence_epoch_and_config_changes_rematerialize(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    changed = replace(_WATERMARK, score_count=_WATERMARK.score_count + 1)
    load = AsyncMock(side_effect=[_WATERMARK, changed, changed, changed])
    materialize = AsyncMock()
    monkeypatch.setattr(efficiency, "_efficiency_evidence_watermark", load)
    monkeypatch.setattr(efficiency, "ensure_efficiency_state", materialize)
    coordinator = EfficiencyStateMaterializer(poll_seconds=0)
    session = _session()

    await coordinator.ensure(session, _ENABLED, now=_NOW)
    await coordinator.ensure(session, _ENABLED, now=_NOW)
    await coordinator.ensure(
        session,
        _ENABLED,
        now=_NOW + timedelta(hours=25),
    )
    await coordinator.ensure(
        session,
        replace(_ENABLED, maximum_factor=1.05),
        now=_NOW + timedelta(hours=25),
    )

    assert materialize.await_count == 4


@pytest.mark.asyncio
async def test_concurrent_requests_share_one_materialization(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def materialize(*_args: object, **_kwargs: object) -> None:
        await asyncio.sleep(0)

    load = AsyncMock(return_value=_WATERMARK)
    materialize_mock = AsyncMock(side_effect=materialize)
    monkeypatch.setattr(efficiency, "_efficiency_evidence_watermark", load)
    monkeypatch.setattr(efficiency, "ensure_efficiency_state", materialize_mock)
    coordinator = EfficiencyStateMaterializer(poll_seconds=30)
    session_a = _session()
    session_b = _session()

    await asyncio.gather(
        coordinator.ensure(
            session_a,
            _ENABLED,
            now=_NOW,
        ),
        coordinator.ensure(
            session_b,
            _ENABLED,
            now=_NOW,
        ),
    )

    load.assert_awaited_once()
    materialize_mock.assert_awaited_once()


@pytest.mark.asyncio
async def test_failed_materialization_is_not_cached(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    load = AsyncMock(return_value=_WATERMARK)
    materialize = AsyncMock(side_effect=[RuntimeError("boom"), None])
    monkeypatch.setattr(efficiency, "_efficiency_evidence_watermark", load)
    monkeypatch.setattr(efficiency, "ensure_efficiency_state", materialize)
    coordinator = EfficiencyStateMaterializer(poll_seconds=30)
    session = _session()

    with pytest.raises(RuntimeError, match="boom"):
        await coordinator.ensure(session, _ENABLED, now=_NOW)
    await coordinator.ensure(session, _ENABLED, now=_NOW)

    assert load.await_count == 2
    assert materialize.await_count == 2


@pytest.mark.asyncio
async def test_disabled_policy_does_not_poll_or_materialize(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    load = AsyncMock(return_value=_WATERMARK)
    materialize = AsyncMock()
    monkeypatch.setattr(efficiency, "_efficiency_evidence_watermark", load)
    monkeypatch.setattr(efficiency, "ensure_efficiency_state", materialize)

    await EfficiencyStateMaterializer().ensure(
        _session(),
        EfficiencyBonusConfig(),
        now=_NOW,
    )

    load.assert_not_awaited()
    materialize.assert_not_awaited()
