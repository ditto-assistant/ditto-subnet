"""SN-485: the rollout catch-up board counts reruns of one artifact once.

The catch-up board names one agent per owner family to re-score at a season
boundary. Like the ledger, it must name the newest run of a rerun artifact,
not its best draw, or the retest would go to a run that can no longer
represent the owner.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import UUID, uuid4

import pytest
from sqlalchemy import true
from sqlalchemy.ext.asyncio import AsyncSession

import ditto.api_server.dueling_seasons as dueling_mod
from ditto.api_models.agent_status import AgentStatus
from ditto.db.models import Agent, EvaluationPayment, Score

_BENCH = 16
_VALIDATORS = ("5" + "1" * 47, "5" + "m" * 47, "5" + "z" * 47)
_T0 = datetime(2026, 11, 1, 12, 0, 0, tzinfo=UTC)


async def _seed(
    session: AsyncSession,
    *,
    hotkey: str,
    coldkey: str,
    sha256: str,
    composite: float,
    created_at: datetime,
) -> UUID:
    agent_id = uuid4()
    async with session.begin():
        session.add(
            Agent(
                agent_id=agent_id,
                miner_hotkey=hotkey,
                name="agent",
                sha256=sha256,
                size_bytes=1024,
                status=AgentStatus.SCORED,
                created_at=created_at,
            )
        )
        await session.flush()
        session.add(
            EvaluationPayment(
                block_hash=f"0x{agent_id.hex}",
                extrinsic_index=0,
                agent_id=agent_id,
                miner_hotkey=hotkey,
                miner_coldkey=coldkey,
                amount_rao=1,
                dest_address="5Destination",
                timestamp=created_at,
            )
        )
        for validator in _VALIDATORS:
            session.add(
                Score(
                    agent_id=agent_id,
                    validator_hotkey=validator,
                    bench_version=_BENCH,
                    run_id=f"run-{validator}",
                    seed=3,
                    composite=composite,
                    tool_mean=composite,
                    memory_mean=composite,
                    median_ms=10,
                    n=350,
                    generated_at=created_at,
                )
            )
    return agent_id


async def test_catchup_board_names_the_newest_rerun_not_the_best_draw(
    session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    hotkey = "5" + "A" * 47
    owner = "5ColdkeyOwner"
    artifact = "d0" * 32
    lucky = await _seed(
        session,
        hotkey=hotkey,
        coldkey=owner,
        sha256=artifact,
        composite=0.95,
        created_at=_T0,
    )
    rerun = await _seed(
        session,
        hotkey=hotkey,
        coldkey=owner,
        sha256=artifact,
        composite=0.90,
        created_at=_T0 + timedelta(hours=1),
    )
    other = await _seed(
        session,
        hotkey="5" + "B" * 47,
        coldkey="5" + "C" * 47,
        sha256="d1" * 32,
        composite=0.80,
        created_at=_T0,
    )
    ranked = [lucky, rerun, other]

    async def _ranked(*_args: Any, **_kwargs: Any) -> list[UUID]:
        return ranked

    async def _scope(*_args: Any, **_kwargs: Any) -> Any:
        return true()

    monkeypatch.setattr("ditto.db.queries.scores.ranked_quorum_agent_ids", _ranked)
    monkeypatch.setattr(
        "ditto.db.queries.dueling_season_window.season_scope_predicate", _scope
    )

    board = await dueling_mod._rollout_catchup_board(session, _BENCH)

    assert board == (rerun, other)


async def test_catchup_board_still_prefers_a_better_distinct_version(
    session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    hotkey = "5" + "A" * 47
    owner = "5ColdkeyOwner"
    older = await _seed(
        session,
        hotkey=hotkey,
        coldkey=owner,
        sha256="e0" * 32,
        composite=0.95,
        created_at=_T0,
    )
    newer = await _seed(
        session,
        hotkey=hotkey,
        coldkey=owner,
        sha256="e1" * 32,
        composite=0.90,
        created_at=_T0 + timedelta(hours=1),
    )

    async def _ranked(*_args: Any, **_kwargs: Any) -> list[UUID]:
        return [older, newer]

    async def _scope(*_args: Any, **_kwargs: Any) -> Any:
        return true()

    monkeypatch.setattr("ditto.db.queries.scores.ranked_quorum_agent_ids", _ranked)
    monkeypatch.setattr(
        "ditto.db.queries.dueling_season_window.season_scope_predicate", _scope
    )

    assert await dueling_mod._rollout_catchup_board(session, _BENCH) == (older,)
