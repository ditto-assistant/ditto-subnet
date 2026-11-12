"""Generator pre-screen batches: settings, opening and expiry.

Pre-screens run a few never-scored generators on a small panel while no round
needs it; their column scores only order the challenger queue. These tests
cover the lane's settings and the batch lifecycle Platform drives itself.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import UUID

import httpx
import pytest
from fastapi import FastAPI
from pydantic import ValidationError
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from ditto.api_models.generator import GeneratorCompetitionSettings
from ditto.api_server.generator_prescreens import (
    PRESCREEN_INDEX_BASE,
    PRESCREEN_TTL,
    advance_prescreens,
)
from ditto.api_server.generator_rounds import GeneratorRoundLoop
from ditto.db.models import GeneratorPrescreen, GeneratorSubmission
from ditto.tests.api_server.endpoints.test_generator_competition import (
    _ADMIN,
    _settings,
)

# The round tests' autouse stand-in for a round-capable validator fleet; a
# batch freezes a bench contract the same way a round does.
from ditto.tests.api_server.endpoints.test_generator_rounds import (  # noqa: F401
    _advance,
    _apply_settings,
    _install,
    _patch_panel,
    _seed_agent,
    _seed_generator,
    round_capable_fleet,
)

_PRESCREENS_URL = "/api/v1/admin/generator-competition/prescreens"


def test_the_lane_ships_off_and_is_bounded() -> None:
    settings = GeneratorCompetitionSettings()
    assert settings.prescreen_enabled is False
    assert (
        settings.prescreen_cases,
        settings.prescreen_panel_size,
        settings.prescreen_columns,
    ) == (16, 3, 4)
    # A revision written before the lane existed reads as off.
    assert (
        GeneratorCompetitionSettings.model_validate(_settings()).prescreen_enabled
        is False
    )
    for field, bad in (
        ("prescreen_cases", 7),
        ("prescreen_cases", 81),
        ("prescreen_panel_size", 0),
        ("prescreen_panel_size", 17),
        ("prescreen_columns", 0),
        ("prescreen_columns", 9),
    ):
        with pytest.raises(ValidationError):
            GeneratorCompetitionSettings.model_validate(_settings(**{field: bad}))


async def _seed_queue(maker: async_sessionmaker[AsyncSession]) -> dict[str, UUID]:
    """Never-scored generators in qualification order: a, b, a's second, c."""
    ids: dict[str, UUID] = {}
    now = datetime.now(UTC)
    for age, (name, owner) in enumerate(
        (("c", "c"), ("a2", "a"), ("b", "b"), ("a", "a")), start=1
    ):
        ids[name] = await _seed_generator(
            maker, hotkey=f"5Prescreen-{name}", owner=f"coldkey:{owner}", salt=name
        )
        async with maker() as session, session.begin():
            row = await session.get(GeneratorSubmission, ids[name])
            assert row is not None
            row.module = f"example.com/{name}"
            row.qualified_at = now - timedelta(hours=age)
    return ids


async def _tick(
    maker: async_sessionmaker[AsyncSession], now: datetime | None = None
) -> list[str]:
    async with maker() as session, session.begin():
        return await advance_prescreens(session, now=now or datetime.now(UTC))


async def _batches(maker: async_sessionmaker[AsyncSession]) -> list[GeneratorPrescreen]:
    async with maker() as session:
        return list(
            await session.scalars(
                select(GeneratorPrescreen).order_by(GeneratorPrescreen.prescreen_index)
            )
        )


async def _prescreen_indexes(
    maker: async_sessionmaker[AsyncSession], ids: dict[str, UUID]
) -> dict[str, int | None]:
    async with maker() as session:
        out: dict[str, int | None] = {}
        for name, submission_id in ids.items():
            row = await session.get(GeneratorSubmission, submission_id)
            assert row is not None
            out[name] = row.prescreen_index
        return out


async def _enable(client: httpx.AsyncClient, **overrides: Any) -> None:
    await _apply_settings(
        client,
        validator_rounds_enabled=True,
        prescreen_enabled=True,
        prescreen_columns=2,
        **overrides,
    )


class TestPrescreenBatches:
    async def test_a_batch_opens_from_the_queue_head_while_idle(
        self,
        app: FastAPI,
        client: httpx.AsyncClient,
        session_maker: async_sessionmaker[AsyncSession],
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """The batch takes the head of the challenger queue, one generator per
        owner, with its own index range, case count and small panel."""
        _install(app, session_maker)
        await _enable(client)
        agents = [await _seed_agent(session_maker, rank) for rank in range(4)]
        _patch_panel(monkeypatch, agents)
        ids = await _seed_queue(session_maker)

        notes = await _tick(session_maker)
        assert notes and "opened with 2 column(s) of 16 cases, panel 3" in notes[-1]
        [batch] = await _batches(session_maker)
        assert batch.prescreen_index >= PRESCREEN_INDEX_BASE
        assert batch.status == "generating"
        assert batch.cases_per_column == 16
        assert [c["submission_id"] for c in batch.columns] == [
            str(ids["a"]),
            str(ids["b"]),
        ]
        assert [p["agent_id"] for p in batch.panel] == [str(a) for a in agents[:3]]
        assert len(batch.seed) == 64
        assert await _prescreen_indexes(session_maker, ids) == {
            "c": None,
            "a2": None,
            "b": batch.prescreen_index,
            "a": batch.prescreen_index,
        }

        # One batch at a time.
        assert await _tick(session_maker) == []
        assert len(await _batches(session_maker)) == 1

        listing = await client.get(_PRESCREENS_URL, headers=_ADMIN)
        assert listing.status_code == 200, listing.text
        body = listing.json()
        assert body["counts_by_status"] == {"generating": 1}
        assert [p["prescreen_index"] for p in body["prescreens"]] == [
            batch.prescreen_index
        ]
        assert [c["counted"] for c in body["prescreens"][0]["columns"]] == [
            False,
            False,
        ]
        assert (await client.get(_PRESCREENS_URL)).status_code == 401

    async def test_nothing_opens_while_off_or_while_a_round_is_in_flight(
        self,
        app: FastAPI,
        client: httpx.AsyncClient,
        session_maker: async_sessionmaker[AsyncSession],
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        _install(app, session_maker)
        await _apply_settings(client, validator_rounds_enabled=True)
        agents = [await _seed_agent(session_maker, rank) for rank in range(3)]
        _patch_panel(monkeypatch, agents)
        await _seed_queue(session_maker)
        assert await _tick(session_maker) == []  # the lane ships off

        # A round in flight keeps the panel: the batch waits for it.
        await _advance(session_maker, 9)  # commits round 3
        await _advance(session_maker, 12)  # opens round 3
        await _enable(client, skip_unchanged_rounds=False)
        assert await _tick(session_maker) == []
        assert await _batches(session_maker) == []

    async def test_an_expired_batch_is_void_and_its_generators_requeue(
        self,
        app: FastAPI,
        client: httpx.AsyncClient,
        session_maker: async_sessionmaker[AsyncSession],
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        _install(app, session_maker)
        await _enable(client)
        agents = [await _seed_agent(session_maker, rank) for rank in range(3)]
        _patch_panel(monkeypatch, agents)
        ids = await _seed_queue(session_maker)
        now = datetime.now(UTC)
        await _tick(session_maker, now)
        [first] = await _batches(session_maker)

        later = now + PRESCREEN_TTL + timedelta(minutes=1)
        notes = await _tick(session_maker, later)
        assert notes[0] == f"prescreen {first.prescreen_index}: void (expired)"
        first, second = await _batches(session_maker)
        assert first.status == "void" and "expired" in (first.void_reason or "")
        # Its generators went back to the queue head and opened the next batch.
        assert second.status == "generating"
        assert second.prescreen_index == first.prescreen_index + 1
        assert [c["submission_id"] for c in second.columns] == [
            str(ids["a"]),
            str(ids["b"]),
        ]
        indexes = await _prescreen_indexes(session_maker, ids)
        assert indexes["a"] == indexes["b"] == second.prescreen_index

    async def test_the_round_loop_drives_the_lane_in_its_own_transaction(
        self,
        app: FastAPI,
        client: httpx.AsyncClient,
        session_maker: async_sessionmaker[AsyncSession],
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        _install(app, session_maker)
        await _enable(client)
        agents = [await _seed_agent(session_maker, rank) for rank in range(3)]
        _patch_panel(monkeypatch, agents)
        await _seed_queue(session_maker)
        loop = GeneratorRoundLoop(app_state=app.state, session_maker=session_maker)
        notes = await loop._advance_prescreens(datetime.now(UTC))
        assert len(notes) == 1 and "opened" in notes[0]
        assert len(await _batches(session_maker)) == 1

        # A failing pre-screen tick is reported, never raised into the loop.
        async def _fail(*_args: Any, **_kwargs: Any) -> list[str]:
            raise RuntimeError("boom")

        monkeypatch.setattr(
            "ditto.api_server.generator_prescreens.advance_prescreens", _fail
        )
        assert await loop._advance_prescreens(datetime.now(UTC)) == [
            "prescreen tick failed: RuntimeError: boom"
        ]
