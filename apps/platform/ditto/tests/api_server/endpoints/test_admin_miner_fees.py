"""Coverage for database-backed miner fee accounting."""

import csv
import io
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
    destination: str = "5PaymentAddress",
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
                dest_address=destination,
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


async def test_address_history_and_complete_csv(
    app: FastAPI,
    client: httpx.AsyncClient,
    session_maker: async_sessionmaker[AsyncSession],
) -> None:
    from ditto.db.models import SubmissionDepositAddressRevision

    _install(app, session_maker)
    app.state.config = replace(
        app.state.config, upload_payment_address="5OriginalAddress"
    )
    async with session_maker() as session, session.begin():
        session.add(
            SubmissionDepositAddressRevision(
                parent_revision=0,
                payment_address="5" + "A" * 47,
                reason="Rotate collection destination",
                actor="operator",
            )
        )
    await _seed_payment(
        session_maker,
        amount_rao=21_000_000_001,
        tao_usd_rate=None,
        age=timedelta(days=60),
        coldkey='=SUM(1,2)\n"quoted"',
        destination="5OriginalAddress",
    )
    await _seed_payment(
        session_maker,
        amount_rao=1,
        tao_usd_rate=Decimal("250.12345678"),
        age=timedelta(days=1),
        coldkey="5Miner",
        destination="5" + "A" * 47,
    )
    body = (await client.get("/api/v1/admin/miner-fees", headers=_HEADERS)).json()
    history = {row["payment_address"]: row for row in body["address_history"]}
    assert history["5OriginalAddress"]["gross_amount_rao"] == 21_000_000_001
    assert history["5OriginalAddress"]["is_current"] is False
    assert history["5" + "A" * 47]["is_current"] is True
    assert (
        sum(row["gross_amount_rao"] for row in history.values())
        == body["gross_amount_rao"]
    )
    assert sum(row["paid_submissions"] for row in body["recent_days"]) == 1

    response = await client.get("/api/v1/admin/miner-fees/export.csv", headers=_HEADERS)
    assert response.status_code == 200
    assert response.headers["cache-control"] == "no-store"
    assert "attachment" in response.headers["content-disposition"]
    rows = list(csv.DictReader(io.StringIO(response.text)))
    assert (
        len(rows) == 2
    )  # Includes the former wallet payment outside the 30-day chart.
    assert rows[0]["amount_tao"] == "21.000000001"
    assert rows[0]["historical_value_usd"] == ""
    assert rows[0]["miner_coldkey"] == "'=" + 'SUM(1,2)\n"quoted"'
    assert rows[0]["destination_address"] == "5OriginalAddress"
    assert rows[0]["timestamp_utc"].endswith("+00:00")
    assert rows[1]["amount_rao"] == "1"
    assert rows[1]["amount_tao"] == "0.000000001"
    assert Decimal(rows[1]["historical_value_usd"]) == Decimal("0.00000025012345678")


async def test_csv_requires_admin_token(
    app: FastAPI, client: httpx.AsyncClient
) -> None:
    app.state.config = replace(app.state.config, admin_api_token=_TOKEN)
    response = await client.get("/api/v1/admin/miner-fees/export.csv")
    assert response.status_code == 401
