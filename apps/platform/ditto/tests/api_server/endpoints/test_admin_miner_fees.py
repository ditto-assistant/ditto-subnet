"""Coverage for database-backed miner fee accounting."""

from collections.abc import AsyncIterator
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from uuid import uuid4

import httpx
from fastapi import FastAPI
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from ditto.api_models.agent_status import AgentStatus
from ditto.api_server.dependencies import get_session
from ditto.db.models import Agent, EvaluationPayment

_TOKEN = "test-admin-token-at-least-32-characters"
_HEADERS = {"Authorization": f"Bearer {_TOKEN}", "X-Admin-Actor": "operator"}
_NOW = datetime.now(UTC)


def _install(app: FastAPI, maker: async_sessionmaker[AsyncSession]) -> None:
    app.state.config = replace(app.state.config, admin_api_token=_TOKEN)

    async def _session() -> AsyncIterator[AsyncSession]:
        async with maker() as session:
            yield session

    app.dependency_overrides[get_session] = _session


async def _seed_payment(
    maker: async_sessionmaker[AsyncSession],
    *,
    amount_rao: int,
    tao_usd_rate: Decimal | None,
    age: timedelta,
    coldkey: str,
) -> None:
    agent_id = uuid4()
    timestamp = _NOW - age
    async with maker() as session, session.begin():
        session.add(
            Agent(
                agent_id=agent_id,
                miner_hotkey=f"5Hotkey{agent_id.hex}",
                name=f"agent-{agent_id.hex}",
                version=1,
                sha256=agent_id.hex * 2,
                status=AgentStatus.EVALUATING,
                created_at=timestamp,
            )
        )
        session.add(
            EvaluationPayment(
                block_hash=f"0x{agent_id.hex}",
                extrinsic_index=0,
                agent_id=agent_id,
                miner_hotkey=f"5Hotkey{agent_id.hex}",
                miner_coldkey=coldkey,
                amount_rao=amount_rao,
                tao_usd_rate=tao_usd_rate,
                dest_address="5PaymentAddress",
                timestamp=timestamp,
            )
        )


async def test_summary_uses_payment_ledger_and_discloses_unpriced_rows(
    app: FastAPI,
    client: httpx.AsyncClient,
    session_maker: async_sessionmaker[AsyncSession],
) -> None:
    _install(app, session_maker)
    await _seed_payment(
        session_maker,
        amount_rao=20_000_000,
        tao_usd_rate=Decimal("250"),
        age=timedelta(days=1),
        coldkey="5ColdkeyA",
    )
    await _seed_payment(
        session_maker,
        amount_rao=30_000_000,
        tao_usd_rate=None,
        age=timedelta(days=2),
        coldkey="5ColdkeyA",
    )

    response = await client.get("/api/v1/admin/miner-fees", headers=_HEADERS)

    assert response.status_code == 200
    body = response.json()
    assert body["paid_submissions"] == 2
    assert body["gross_amount_rao"] == 50_000_000
    assert body["priced_submissions"] == 1
    assert body["unpriced_submissions"] == 1
    assert Decimal(str(body["gross_value_usd"])) == Decimal("5")
    assert body["unique_paying_coldkeys"] == 1
    assert sum(day["paid_submissions"] for day in body["recent_days"]) == 2


async def test_summary_requires_admin_token(
    app: FastAPI,
    client: httpx.AsyncClient,
) -> None:
    app.state.config = replace(app.state.config, admin_api_token=_TOKEN)
    response = await client.get("/api/v1/admin/miner-fees")
    assert response.status_code == 401
