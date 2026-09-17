"""Read-only Backroom projection of the finalized-block confirmation seed anchors."""

from __future__ import annotations

from collections.abc import AsyncIterator
from dataclasses import replace
from datetime import UTC, datetime
from uuid import UUID, uuid4

import httpx
import pytest
from fastapi import FastAPI
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from ditto.api_models.agent_status import AgentStatus
from ditto.api_server import crn as crn_mod
from ditto.api_server.dependencies import get_session
from ditto.db.models import Agent, ConfirmationSeedAnchor

pytestmark = pytest.mark.asyncio

_ADMIN_TOKEN = "test-admin-token-at-least-32-characters"
_HEADERS = {"Authorization": f"Bearer {_ADMIN_TOKEN}"}
_URL = "/api/v1/admin/confirmation-seed-anchors"
_HASH = "0x" + "ab" * 32
_NOW = datetime(2026, 9, 13, 12, 0, tzinfo=UTC)


def _install(app: FastAPI, maker: async_sessionmaker[AsyncSession]) -> None:
    app.state.config = replace(app.state.config, admin_api_token=_ADMIN_TOKEN)
    app.state.session_maker = maker

    async def _session() -> AsyncIterator[AsyncSession]:
        async with maker() as session:
            yield session

    app.dependency_overrides[get_session] = _session


async def _seed_reign(
    maker: async_sessionmaker[AsyncSession],
    *,
    name: str,
    bench_version: int,
    ready_block: int,
    pinned: bool,
) -> UUID:
    agent_id = uuid4()
    async with maker() as session, session.begin():
        session.add(
            Agent(
                agent_id=agent_id,
                miner_hotkey=f"5Miner{name}",
                name=name,
                sha256=f"{len(name):02d}" * 32,
                status=AgentStatus.SCORED,
                created_at=_NOW,
            )
        )
        await session.flush()
        session.add(
            ConfirmationSeedAnchor(
                champion_agent_id=agent_id,
                bench_version=bench_version,
                ready_block=ready_block,
                anchor_block=ready_block + crn_mod.CRN_ANCHOR_BLOCK_DELTA,
                anchor_block_hash=_HASH if pinned else None,
                pinned_at=_NOW if pinned else None,
            )
        )
    return agent_id


async def test_requires_admin(
    app: FastAPI,
    client: httpx.AsyncClient,
    session_maker: async_sessionmaker[AsyncSession],
) -> None:
    _install(app, session_maker)
    missing = await client.get(_URL)
    assert missing.status_code == 401, missing.text
    wrong = await client.get(_URL, headers={"Authorization": "Bearer nope"})
    assert wrong.status_code == 401, wrong.text


async def test_lists_waiting_and_pinned_reigns_of_the_active_version(
    app: FastAPI,
    client: httpx.AsyncClient,
    session_maker: async_sessionmaker[AsyncSession],
) -> None:
    """The ledger serves pinned anchors only; this surface is where a reign in
    its finality wait is visible, oldest anchor block first."""
    _install(app, session_maker)
    empty = await client.get(_URL, headers=_HEADERS)
    assert empty.status_code == 200, empty.text
    body = empty.json()
    active = body["bench_version"]
    assert body["items"] == []
    assert body == {
        "bench_version": active,
        "binding_active": crn_mod.crn_block_binding_active(active),
        "binding_floor_bench_version": crn_mod.CRN_BLOCK_BINDING_MIN_BENCH_VERSION,
        "anchor_block_delta": crn_mod.CRN_ANCHOR_BLOCK_DELTA,
        "items": [],
        "count": 0,
        "pinned_count": 0,
        "waiting_count": 0,
    }

    later = await _seed_reign(
        session_maker, name="later", bench_version=active, ready_block=500, pinned=False
    )
    earlier = await _seed_reign(
        session_maker,
        name="earlier",
        bench_version=active,
        ready_block=100,
        pinned=True,
    )
    # Another version's reign never leaks into this version's list.
    await _seed_reign(
        session_maker,
        name="other",
        bench_version=active + 1,
        ready_block=1,
        pinned=True,
    )

    response = await client.get(_URL, headers=_HEADERS)
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["count"] == 2
    assert body["pinned_count"] == 1
    assert body["waiting_count"] == 1
    assert [item["champion_agent_id"] for item in body["items"]] == [
        str(earlier),
        str(later),
    ]
    pinned, waiting = body["items"]
    assert pinned["pinned"] is True
    assert pinned["anchor_block_hash"] == _HASH
    assert pinned["pinned_at"] is not None
    assert pinned["champion_name"] == "earlier"
    assert pinned["champion_miner_hotkey"] == "5Minerearlier"
    assert pinned["anchor_block"] == 100 + crn_mod.CRN_ANCHOR_BLOCK_DELTA
    assert waiting["pinned"] is False
    assert waiting["anchor_block_hash"] is None
    assert waiting["pinned_at"] is None
    assert waiting["ready_block"] == 500

    # An explicit version lists that version instead of the active one.
    other = await client.get(
        _URL, headers=_HEADERS, params={"bench_version": active + 1}
    )
    assert other.status_code == 200, other.text
    assert other.json()["bench_version"] == active + 1
    assert [item["champion_name"] for item in other.json()["items"]] == ["other"]
