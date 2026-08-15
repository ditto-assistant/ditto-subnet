from __future__ import annotations

from datetime import UTC, datetime, timedelta

import httpx
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from ditto.api_server.validator_names import (
    TaostatsValidatorNames,
    ValidatorNamesConfig,
)
from ditto.db.queries.validator_names import (
    load_validator_name_cache,
    replace_validator_name_cache,
)


async def test_successful_snapshot_replaces_durable_validator_name_cache(
    session: AsyncSession,
) -> None:
    first = datetime(2026, 8, 15, 12, tzinfo=UTC)
    await replace_validator_name_cache(
        session,
        names={"5Alice": "Alice", "5Removed": "Removed"},
        stake_weights={"5Alice": 12.5},
        refreshed_at=first,
    )
    await session.commit()

    assert await load_validator_name_cache(session) == (
        {"5Alice": "Alice", "5Removed": "Removed"},
        {"5Alice": 12.5},
        first,
    )

    second = first + timedelta(hours=1)
    await replace_validator_name_cache(
        session,
        names={"5Alice": "Alice renamed"},
        stake_weights={},
        refreshed_at=second,
    )
    await session.commit()

    assert await load_validator_name_cache(session) == (
        {"5Alice": "Alice renamed"},
        {},
        second,
    )


async def test_refresher_hydrates_last_snapshot_before_upstream_retry(
    session: AsyncSession,
    session_maker: async_sessionmaker[AsyncSession],
) -> None:
    refreshed_at = datetime(2026, 7, 31, 10, 56, 23, tzinfo=UTC)
    await replace_validator_name_cache(
        session,
        names={"5Alice": "Alice"},
        stake_weights={},
        refreshed_at=refreshed_at,
    )
    await session.commit()

    async def insufficient_credits(_: httpx.Request) -> httpx.Response:
        return httpx.Response(402, json={"message": "Insufficient credits"})

    client = httpx.AsyncClient(transport=httpx.MockTransport(insufficient_credits))
    cache = TaostatsValidatorNames(
        ValidatorNamesConfig(url="https://api.taostats.io/names", api_key="key"),
        client,
    )
    await cache.start(session_maker)
    try:
        snapshot = cache.snapshot(["5Alice"], now=refreshed_at + timedelta(days=15))
    finally:
        await cache.aclose()
        await client.aclose()

    assert snapshot.status == "stale"
    assert snapshot.refreshed_at == refreshed_at
    assert snapshot.names == {"5Alice": "Alice"}
    assert snapshot.stake_weights == {}
