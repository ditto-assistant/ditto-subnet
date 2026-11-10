"""The public dueling-season gate on queued scoring (bench v16+).

While no season is open for the bench version the queue scores at, the
dashboard says so on every waiting row and on the rollout status, with an
approximate time the next season opens at the next round boundary, and null
rather than a guess when the schedule is unknown.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from typing import Any
from uuid import uuid4

import httpx
import pytest
from fastapi import FastAPI
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

import ditto.api_server.dueling_season_gate as gate_mod
from ditto.api_models.agent_status import AgentStatus
from ditto.api_models.generator_rounds import GeneratorRoundSchedule
from ditto.api_server.dueling_season_gate import (
    DuelingSeasonGate,
    dueling_season_gate,
    epoch_starts_at,
    season_opening_epoch,
)
from ditto.api_server.dueling_seasons import DuelingTarget
from ditto.api_server.endpoints import public as public_endpoint
from ditto.api_server.generator_rounds import CHAIN_BLOCK_SECONDS
from ditto.db.models import DuelingSeason
from ditto.tests.api_server.endpoints.test_public import (
    _MINER_A,
    SCREENING_POLICY_VERSION,
    _activate_era,
    _install_db,
    _seed_agent,
)
from ditto_screening_protocol.dueling import season_commitment, season_seed

_NOW = datetime(2026, 11, 10, 22, 0, tzinfo=UTC)
_BLOCK_HASH = "0x" + "7c" * 32


def _schedule(**overrides: Any) -> GeneratorRoundSchedule:
    """Round 10 of 4-epoch rounds, one epoch in; the next round opens at 44."""
    fields: dict[str, Any] = {
        "mode": "on",
        "epochs_per_round": 4,
        "subnet_epoch_index": 41,
        "current_round": 10,
        "next_round_epoch": 44,
        "tempo": 360,
        "block": 10_000,
        "next_epoch_block": 10_100,
        "observed_at": _NOW,
    }
    return GeneratorRoundSchedule(**{**fields, **overrides})


def _versions(
    monkeypatch: pytest.MonkeyPatch,
    *,
    active: int,
    desired: int | None = None,
    blocked: str | None = None,
) -> None:
    async def _active(*_args: Any, **_kwargs: Any) -> int:
        return active

    async def _rollout(*_args: Any, **_kwargs: Any) -> Any:
        return None if desired is None else SimpleNamespace(desired_version=desired)

    async def _target(*_args: Any, **_kwargs: Any) -> DuelingTarget:
        version = desired if desired is not None and desired > active else active
        return DuelingTarget(
            bench_version=version,
            shadow=version != active,
            blocked=blocked,
            live_versions=frozenset({active, version}),
        )

    monkeypatch.setattr(gate_mod, "active_bench_version", _active)
    monkeypatch.setattr(gate_mod, "open_rollout", _rollout)
    monkeypatch.setattr(gate_mod, "dueling_target", _target)


async def _season(
    maker: async_sessionmaker[AsyncSession],
    *,
    bench_version: int,
    season_index: int,
    status: str,
) -> None:
    secret = b"\x02" * 32
    opened = status == "open"
    async with maker() as session, session.begin():
        session.add(
            DuelingSeason(
                id=uuid4(),
                bench_version=bench_version,
                season_index=season_index,
                shadow=False,
                status=status,
                commitment=season_commitment(season_index, secret),
                secret=secret,
                committed_at=_NOW,
                rounds_per_season=4,
                anchor_block=1 if opened else None,
                anchor_block_hash=_BLOCK_HASH if opened else None,
                season_seed=(
                    season_seed(season_index, secret, _BLOCK_HASH) if opened else None
                ),
                columns=[] if opened else None,
                closes_at_round=season_index + 4 if opened else None,
                void_reason="season boundary passed" if status == "void" else None,
                detail={},
            )
        )


class TestScheduleEstimate:
    def test_next_round_boundary_by_default(self) -> None:
        assert season_opening_epoch(_schedule(), None) == 44

    def test_a_later_committed_round_counts_whole_rounds_ahead(self) -> None:
        assert season_opening_epoch(_schedule(), 12) == 48

    def test_unknown_or_passed_boundary_is_none(self) -> None:
        assert season_opening_epoch(None, None) is None
        assert season_opening_epoch(_schedule(mode="off"), None) is None
        assert season_opening_epoch(_schedule(next_round_epoch=None), None) is None
        assert season_opening_epoch(_schedule(), 10) is None

    def test_epoch_time_follows_the_next_epoch_block_then_tempo(self) -> None:
        # Epoch 42 fires at block 10_100; 44 is two tempos later.
        blocks = 10_100 + 2 * 360 - 10_000
        assert epoch_starts_at(_schedule(), 44, now=_NOW) == _NOW + timedelta(
            seconds=blocks * CHAIN_BLOCK_SECONDS
        )

    def test_missing_input_or_stale_estimate_is_none(self) -> None:
        assert epoch_starts_at(_schedule(next_epoch_block=None), 44, now=_NOW) is None
        assert epoch_starts_at(_schedule(), None, now=_NOW) is None
        assert epoch_starts_at(_schedule(), 41, now=_NOW) is None
        later = _NOW + timedelta(days=1)
        assert epoch_starts_at(_schedule(), 44, now=later) is None


class TestDuelingSeasonGate:
    async def test_below_the_dueling_floor_there_is_no_gate(
        self, session: AsyncSession, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _versions(monkeypatch, active=15)
        assert (
            await dueling_season_gate(session, schedule=_schedule(), now=_NOW) is None
        )

    async def test_open_season_is_not_a_wait(
        self,
        session_maker: async_sessionmaker[AsyncSession],
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        _versions(monkeypatch, active=16)
        await _season(session_maker, bench_version=16, season_index=9, status="open")
        async with session_maker() as session:
            gate = await dueling_season_gate(session, schedule=_schedule(), now=_NOW)
        assert gate == DuelingSeasonGate(16, True, None, None)
        assert public_endpoint._public_scoring_wait(gate) is None

    async def test_closed_season_estimates_the_committed_boundary(
        self,
        session_maker: async_sessionmaker[AsyncSession],
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        _versions(monkeypatch, active=16)
        await _season(session_maker, bench_version=16, season_index=9, status="void")
        await _season(
            session_maker, bench_version=16, season_index=11, status="committed"
        )
        async with session_maker() as session:
            gate = await dueling_season_gate(session, schedule=_schedule(), now=_NOW)
        assert gate is not None and not gate.open
        assert gate.bench_version == 16 and gate.next_round_epoch == 44
        assert gate.next_opens_at == epoch_starts_at(_schedule(), 44, now=_NOW)

    async def test_rollout_scores_at_the_desired_version(
        self,
        session_maker: async_sessionmaker[AsyncSession],
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        # The active version's open season does not serve desired-era leases.
        _versions(monkeypatch, active=16, desired=18)
        await _season(session_maker, bench_version=16, season_index=9, status="open")
        async with session_maker() as session:
            gate = await dueling_season_gate(session, schedule=_schedule(), now=_NOW)
        assert gate is not None and not gate.open and gate.bench_version == 18
        assert gate.next_round_epoch == 44

    async def test_unknown_schedule_has_no_estimate(
        self, session: AsyncSession, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _versions(monkeypatch, active=16)
        gate = await dueling_season_gate(session, schedule=None, now=_NOW)
        assert gate == DuelingSeasonGate(16, False, None, None)
        wait = public_endpoint._public_scoring_wait(gate)
        assert wait is not None
        assert wait.model_dump() == {
            "reason": "dueling_season_closed",
            "bench_version": 16,
            "next_season_opens_at": None,
            "next_round_epoch": None,
        }

    async def test_a_blocked_target_names_no_boundary(
        self, session: AsyncSession, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _versions(monkeypatch, active=16, blocked="dueling_enabled is false")
        gate = await dueling_season_gate(session, schedule=_schedule(), now=_NOW)
        assert gate == DuelingSeasonGate(16, False, None, None)


class TestPublicProjection:
    @pytest.fixture
    def closed_gate(self, monkeypatch: pytest.MonkeyPatch) -> DuelingSeasonGate:
        gate = DuelingSeasonGate(18, False, 44, _NOW + timedelta(hours=2))

        async def _gate(*_args: Any, **_kwargs: Any) -> DuelingSeasonGate:
            return gate

        monkeypatch.setattr(public_endpoint, "dueling_season_gate", _gate)
        return gate

    @pytest.mark.usefixtures("closed_gate")
    async def test_waiting_rows_carry_the_season_wait(
        self,
        app: FastAPI,
        client: httpx.AsyncClient,
        session_maker: async_sessionmaker[AsyncSession],
    ) -> None:
        agent_id = await _seed_agent(
            session_maker,
            miner=_MINER_A,
            status=AgentStatus.EVALUATING,
            screening_policy_version=SCREENING_POLICY_VERSION,
        )
        await _seed_agent(session_maker, miner=_MINER_A, name="uploaded")
        await _activate_era(session_maker)
        _install_db(app, session_maker)

        wait = {
            "reason": "dueling_season_closed",
            "bench_version": 18,
            "next_season_opens_at": "2026-11-11T00:00:00Z",
            "next_round_epoch": 44,
        }
        entries = (await client.get("/api/v1/public/activity")).json()["entries"]
        by_status = {entry["status"]: entry for entry in entries}
        assert by_status["waiting_validator"]["agent_id"] == agent_id
        assert by_status["waiting_validator"]["scoring_wait"] == wait
        assert by_status["waiting_screening"]["scoring_wait"] is None

        ops = (await client.get("/api/v1/public/operations")).json()
        [ops_row] = [
            entry
            for entry in ops["activity"]["entries"]
            if entry["agent_id"] == agent_id
        ]
        assert ops_row["scoring_wait"] == wait

        summary = (await client.get(f"/api/v1/public/agent/{agent_id}/summary")).json()
        assert summary["scoring_wait"] == wait

    @pytest.mark.usefixtures("closed_gate")
    async def test_rollout_status_publishes_the_season_state(
        self,
        app: FastAPI,
        client: httpx.AsyncClient,
        session_maker: async_sessionmaker[AsyncSession],
    ) -> None:
        await _activate_era(session_maker)
        _install_db(app, session_maker)
        rollout = (await client.get("/api/v1/public/bench/rollout")).json()
        assert rollout["dueling_season"] == {
            "open": False,
            "bench_version": 18,
            "next_opens_at": "2026-11-11T00:00:00Z",
            "next_round_epoch": 44,
        }

    async def test_below_the_floor_rollout_status_has_no_season(
        self,
        app: FastAPI,
        client: httpx.AsyncClient,
        session_maker: async_sessionmaker[AsyncSession],
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        async def _none(*_args: Any, **_kwargs: Any) -> None:
            return None

        monkeypatch.setattr(public_endpoint, "dueling_season_gate", _none)
        await _activate_era(session_maker)
        _install_db(app, session_maker)
        rollout = (await client.get("/api/v1/public/bench/rollout")).json()
        assert rollout["dueling_season"] is None
