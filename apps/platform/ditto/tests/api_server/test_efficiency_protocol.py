"""Consensus activation boundaries for bounded efficiency factors."""

from __future__ import annotations

from datetime import UTC, datetime
from types import SimpleNamespace
from typing import cast
from unittest.mock import AsyncMock
from uuid import UUID, uuid4

import pytest
from fastapi import Request

import ditto.api_server.endpoints.scoring as scoring_mod
from ditto.api_models import LedgerEntry
from ditto.api_models.agent_status import AgentStatus

_MINER = "5DhaT8U7LVwnnJNUU8VL1XEipicatoaDVVq7cHo227gogVZm"
_VALIDATOR = "5GrwvaEF5zXb26Fz9rcQpDWS57CtERHpNehXCPcNoHGKutQY"


def test_v21_factor_gate_preserves_legacy_bonus_behavior() -> None:
    legacy_id = uuid4()
    factor_id = uuid4()

    legacy, factors = scoring_mod._fleet_safe_efficiency_adjustments(
        {legacy_id: 0.05},
        {factor_id: 0.85},
        factor_fleet_ready=False,
    )
    assert legacy == {legacy_id: 0.05}
    assert factors == {}

    legacy, factors = scoring_mod._fleet_safe_efficiency_adjustments(
        {legacy_id: 0.05},
        {factor_id: 0.85},
        factor_fleet_ready=True,
    )
    assert legacy == {legacy_id: 0.05}
    assert factors == {factor_id: 0.85}


def test_stale_cache_strips_v21_factor_but_preserves_legacy_bonus() -> None:
    now = datetime.now(UTC)

    def _entry(
        agent_id: UUID,
        *,
        bonus: float | None = None,
        factor: float | None = None,
        effective: float | None = None,
    ) -> LedgerEntry:
        return LedgerEntry(
            miner_hotkey=_MINER,
            agent_id=agent_id,
            composite=0.8,
            n=114,
            first_seen=now,
            sha256="ab" * 32,
            size_bytes=123,
            run_id=f"run-{agent_id}",
            seed=42,
            validator_hotkey=_VALIDATOR,
            bench_version=9,
            signature=None,
            score_proofs=[],
            composite_stderr=None,
            confirmation_composites=None,
            confirmation_seeds=None,
            efficiency_bonus=bonus,
            efficiency_factor=factor,
            effective_composite=effective,
            status=AgentStatus.SCORED,
        )

    legacy_id = uuid4()
    factor_id = uuid4()
    request = cast(
        Request,
        SimpleNamespace(
            app=SimpleNamespace(
                state=SimpleNamespace(
                    ledger_snapshot=scoring_mod._LedgerSnapshot(
                        entries=[
                            _entry(legacy_id, bonus=0.05, effective=0.84),
                            _entry(factor_id, factor=0.85, effective=0.68),
                        ],
                        generated_at=now,
                        active_bench_version=9,
                    )
                )
            )
        ),
    )

    response = scoring_mod._serve_last_known(
        request, _VALIDATOR, RuntimeError("db down")
    )
    by_id = {entry.agent_id: entry for entry in response.entries}
    assert by_id[legacy_id].efficiency_bonus == pytest.approx(0.05)
    assert by_id[legacy_id].effective_composite == pytest.approx(0.84)
    assert by_id[factor_id].efficiency_factor is None
    assert by_id[factor_id].effective_composite is None


def _factor_entry(
    agent_id: UUID,
    *,
    factor: float,
    curve_version: int | None,
    now: datetime,
) -> LedgerEntry:
    return LedgerEntry(
        miner_hotkey=_MINER,
        agent_id=agent_id,
        composite=0.8,
        n=114,
        first_seen=now,
        sha256="ab" * 32,
        size_bytes=123,
        run_id=f"run-{agent_id}",
        seed=42,
        validator_hotkey=_VALIDATOR,
        bench_version=9,
        signature=None,
        score_proofs=[],
        composite_stderr=None,
        confirmation_composites=None,
        confirmation_seeds=None,
        efficiency_bonus=None,
        efficiency_factor=factor,
        efficiency_curve_version=curve_version,
        effective_composite=0.8,
        status=AgentStatus.SCORED,
    )


def _share_request(requester: object) -> Request:
    class _SessionCtx:
        def __init__(self, session: object) -> None:
            self._session = session

        async def __aenter__(self) -> object:
            return self._session

        async def __aexit__(self, *_args: object) -> None:
            return None

    session = SimpleNamespace(get=AsyncMock(return_value=requester))
    return cast(
        Request,
        SimpleNamespace(
            app=SimpleNamespace(
                state=SimpleNamespace(session_maker=lambda: _SessionCtx(session))
            )
        ),
    )


@pytest.mark.asyncio
async def test_v4_snapshot_is_not_shared_with_a_protocol_24_requester() -> None:
    now = datetime.now(UTC)
    snapshot = scoring_mod._LedgerSnapshot(
        entries=[
            _factor_entry(uuid4(), factor=1.5, curve_version=4, now=now),
        ],
        generated_at=now,
        active_bench_version=9,
    )
    request = _share_request(SimpleNamespace(protocol_version=24, seen_at=now))

    assert not await scoring_mod._snapshot_can_be_shared(
        request, snapshot, _VALIDATOR, now=now
    )


@pytest.mark.asyncio
async def test_v4_snapshot_is_shared_with_a_fresh_protocol_25_requester() -> None:
    now = datetime.now(UTC)
    snapshot = scoring_mod._LedgerSnapshot(
        entries=[
            _factor_entry(uuid4(), factor=1.5, curve_version=4, now=now),
        ],
        generated_at=now,
        active_bench_version=9,
    )
    request = _share_request(SimpleNamespace(protocol_version=25, seen_at=now))

    assert await scoring_mod._snapshot_can_be_shared(
        request, snapshot, _VALIDATOR, now=now
    )


@pytest.mark.asyncio
async def test_frozen_v3_snapshot_is_shared_with_a_protocol_21_requester() -> None:
    now = datetime.now(UTC)
    snapshot = scoring_mod._LedgerSnapshot(
        entries=[
            _factor_entry(uuid4(), factor=1.10, curve_version=3, now=now),
        ],
        generated_at=now,
        active_bench_version=9,
    )
    request = _share_request(SimpleNamespace(protocol_version=21, seen_at=now))

    assert await scoring_mod._snapshot_can_be_shared(
        request, snapshot, _VALIDATOR, now=now
    )


@pytest.mark.asyncio
async def test_factor_snapshot_without_curve_version_is_not_shared() -> None:
    now = datetime.now(UTC)
    snapshot = scoring_mod._LedgerSnapshot(
        entries=[
            _factor_entry(uuid4(), factor=1.5, curve_version=None, now=now),
        ],
        generated_at=now,
        active_bench_version=9,
    )
    request = _share_request(SimpleNamespace(protocol_version=25, seen_at=now))

    assert not await scoring_mod._snapshot_can_be_shared(
        request, snapshot, _VALIDATOR, now=now
    )
