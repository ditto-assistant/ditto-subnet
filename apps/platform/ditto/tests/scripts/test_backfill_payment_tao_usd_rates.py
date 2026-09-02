from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from uuid import uuid4

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

import scripts.backfill_payment_tao_usd_rates as backfill
from ditto.api_models.agent_status import AgentStatus
from ditto.db.models import Agent, EvaluationPayment


def _payment(at: datetime, suffix: str = "a") -> backfill.Payment:
    return backfill.Payment(f"0x{suffix}", 0, at)


def test_parse_price_points_rejects_malformed_or_unsafe_rates() -> None:
    with pytest.raises(ValueError, match="prices array"):
        backfill._parse_price_points({})
    with pytest.raises(ValueError, match="finite and positive"):
        backfill._parse_price_points({"prices": [[1_700_000_000_000, 0]]})
    with pytest.raises(ValueError, match="duplicate"):
        backfill._parse_price_points(
            {"prices": [[1_700_000_000_000, 200], [1_700_000_000_000, 201]]}
        )


def test_build_updates_uses_nearest_historical_point_and_db_precision() -> None:
    noon = datetime(2026, 7, 15, 12, tzinfo=UTC)
    points = [
        backfill.PricePoint(noon - timedelta(hours=1), Decimal("199.123456784")),
        backfill.PricePoint(noon + timedelta(hours=2), Decimal("205")),
    ]

    updates = backfill._build_updates([_payment(noon)], points)

    assert updates == [
        backfill.PlannedUpdate(
            block_hash="0xa",
            extrinsic_index=0,
            payment_timestamp="2026-07-15T12:00:00+00:00",
            tao_usd_rate="199.12345678",
            source_timestamp="2026-07-15T11:00:00+00:00",
            source_distance_seconds=3600,
        )
    ]


def test_build_updates_fails_closed_when_history_does_not_cover_payment() -> None:
    paid_at = datetime(2026, 7, 15, tzinfo=UTC)
    points = [
        backfill.PricePoint(paid_at - timedelta(days=2), Decimal("200")),
    ]

    with pytest.raises(ValueError, match="is 172800s away"):
        backfill._build_updates([_payment(paid_at)], points)


def test_load_plan_rejects_duplicates_and_unexpected_source(tmp_path) -> None:
    item = backfill.PlannedUpdate(
        block_hash="0xa",
        extrinsic_index=0,
        payment_timestamp="2026-07-15T12:00:00+00:00",
        tao_usd_rate="200.00000000",
        source_timestamp="2026-07-15T12:00:00+00:00",
        source_distance_seconds=0,
    )
    path = tmp_path / "plan.json"
    path.write_text(
        json.dumps(
            {
                "version": 1,
                "source": backfill._COINGECKO_URL,
                "updates": [item.__dict__, item.__dict__],
            }
        )
    )
    with pytest.raises(ValueError, match="duplicate payment"):
        backfill._load_plan(path)

    path.write_text(
        json.dumps({"version": 1, "source": "https://example.com", "updates": []})
    )
    with pytest.raises(ValueError, match="unexpected price source"):
        backfill._load_plan(path)


async def test_apply_plan_updates_only_null_rate_and_is_idempotent(
    tmp_path,
    session_maker: async_sessionmaker[AsyncSession],
    monkeypatch,
) -> None:
    paid_at = datetime(2026, 7, 15, 12, tzinfo=UTC)
    agent_id = uuid4()
    async with session_maker() as session, session.begin():
        session.add(
            Agent(
                agent_id=agent_id,
                miner_hotkey=f"5Hotkey{agent_id.hex}",
                name="legacy-payment",
                version=1,
                sha256=agent_id.hex * 2,
                status=AgentStatus.EVALUATING,
                created_at=paid_at,
            )
        )
        session.add(
            EvaluationPayment(
                block_hash=f"0x{agent_id.hex}",
                extrinsic_index=0,
                agent_id=agent_id,
                miner_hotkey=f"5Hotkey{agent_id.hex}",
                miner_coldkey="5Coldkey",
                amount_rao=40_000_000,
                tao_usd_rate=None,
                dest_address="5PaymentAddress",
                timestamp=paid_at,
            )
        )

    item = backfill.PlannedUpdate(
        block_hash=f"0x{agent_id.hex}",
        extrinsic_index=0,
        payment_timestamp=paid_at.isoformat(),
        tao_usd_rate="201.12345678",
        source_timestamp=(paid_at - timedelta(minutes=10)).isoformat(),
        source_distance_seconds=600,
    )
    path = tmp_path / "plan.json"
    path.write_text(
        json.dumps(
            {
                "version": 1,
                "source": backfill._COINGECKO_URL,
                "updates": [item.__dict__],
            }
        )
    )

    class Engine:
        async def dispose(self) -> None:
            return None

    monkeypatch.setattr(backfill, "create_db_engine", Engine)
    monkeypatch.setattr(backfill, "create_session_maker", lambda _engine: session_maker)

    assert await backfill._apply_plan(path) == 0
    assert await backfill._apply_plan(path) == 0
    async with session_maker() as session:
        rate = await session.scalar(
            select(EvaluationPayment.tao_usd_rate).where(
                EvaluationPayment.block_hash == item.block_hash
            )
        )
    assert rate == Decimal("201.12345678")
