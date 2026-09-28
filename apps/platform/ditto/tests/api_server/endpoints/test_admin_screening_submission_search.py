"""Server-side search filters on ``GET /admin/screening-submissions`` (#560).

Operators know a submission by its name, miner, artifact, status, or failure
class, never by its UUID; these filters resolve that in one bounded call
instead of paging the whole table and filtering client-side.
"""

from collections.abc import AsyncIterator
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4

import httpx
import pytest
from fastapi import FastAPI
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from ditto.api_server.dependencies import get_session
from ditto.db.models import Agent, AgentStatus, EvaluationPayment

_TOKEN = "test-admin-token-at-least-32-characters"
_HEADERS = {"Authorization": f"Bearer {_TOKEN}"}
_URL = "/api/v1/admin/screening-submissions"
_T0 = datetime(2026, 9, 1, 12, tzinfo=UTC)


@pytest.fixture
def maker(
    app: FastAPI, session_maker: async_sessionmaker[AsyncSession]
) -> async_sessionmaker[AsyncSession]:
    app.state.config = replace(app.state.config, admin_api_token=_TOKEN)

    async def _session() -> AsyncIterator[AsyncSession]:
        async with session_maker() as session:
            yield session

    app.dependency_overrides[get_session] = _session
    return session_maker


async def _seed(
    maker: async_sessionmaker[AsyncSession],
    *,
    name: str,
    version: int = 1,
    hotkey: str = "5HotAlpha",
    coldkey: str | None = "5ColdAlpha",
    sha256: str | None = None,
    status: AgentStatus = AgentStatus.SCORED,
    reason_code: str | None = None,
    created_at: datetime = _T0,
) -> UUID:
    agent_id = uuid4()
    async with maker() as session, session.begin():
        session.add(
            Agent(
                agent_id=agent_id,
                miner_hotkey=hotkey,
                name=name,
                version=version,
                sha256=sha256 or agent_id.hex * 2,
                status=status,
                screening_reason_code=reason_code,
                created_at=created_at,
                screening_policy_version=8,
            )
        )
        if coldkey is not None:
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
    return agent_id


_Param = str | int | list[str]


async def _search(client: httpx.AsyncClient, **params: _Param) -> dict:
    response = await client.get(_URL, headers=_HEADERS, params=params)
    assert response.status_code == 200, response.text
    return response.json()


def _names(body: dict) -> list[str]:
    return [f"{item['agent_name']}@{item['agent_version']}" for item in body["items"]]


@pytest.mark.asyncio
async def test_exact_name_returns_every_version_and_a_filtered_count(
    client: httpx.AsyncClient, maker: async_sessionmaker[AsyncSession]
) -> None:
    await _seed(maker, name="moonlight_v1", version=1, created_at=_T0)
    await _seed(
        maker, name="moonlight_v1", version=2, created_at=_T0 + timedelta(hours=1)
    )
    await _seed(maker, name="moonlight_v10", created_at=_T0 + timedelta(hours=2))
    for index in range(5):
        await _seed(maker, name=f"other{index}", created_at=_T0 + timedelta(days=1))

    body = await _search(client, agent_name="moonlight_v1", limit=1)

    # count is the filtered total, not the table total, so offsets stay useful.
    assert body["count"] == 2
    assert _names(body) == ["moonlight_v1@2"]
    page_two = await _search(client, agent_name="moonlight_v1", limit=1, offset=1)
    assert _names(page_two) == ["moonlight_v1@1"]
    unfiltered = await _search(client)
    assert unfiltered["count"] == 8


@pytest.mark.asyncio
async def test_name_prefix_matches_literally_and_escapes_like_metacharacters(
    client: httpx.AsyncClient, maker: async_sessionmaker[AsyncSession]
) -> None:
    await _seed(maker, name="moon_v1")
    await _seed(maker, name="moonXv1", created_at=_T0 + timedelta(minutes=1))
    await _seed(maker, name="moon%v1", created_at=_T0 + timedelta(minutes=2))
    await _seed(maker, name="moon\\v1", created_at=_T0 + timedelta(minutes=3))
    await _seed(maker, name="moonlight", created_at=_T0 + timedelta(minutes=4))
    await _seed(maker, name="honeymoon", created_at=_T0 + timedelta(minutes=5))

    wide = await _search(client, agent_name_prefix="moon")
    assert wide["count"] == 5
    assert "honeymoon@1" not in _names(wide)

    # ``_`` and ``%`` are literal characters, not single/multi wildcards.
    assert _names(await _search(client, agent_name_prefix="moon_")) == ["moon_v1@1"]
    assert _names(await _search(client, agent_name_prefix="moon%")) == ["moon%v1@1"]
    assert _names(await _search(client, agent_name_prefix="moon\\")) == ["moon\\v1@1"]
    assert (await _search(client, agent_name_prefix="%"))["count"] == 0


@pytest.mark.asyncio
async def test_identity_filters_and_combinations_are_and_combined(
    client: httpx.AsyncClient, maker: async_sessionmaker[AsyncSession]
) -> None:
    shared_sha = "ab" * 32
    a1 = await _seed(maker, name="a1", hotkey="5HotAlpha", sha256=shared_sha)
    await _seed(
        maker,
        name="b1",
        hotkey="5HotBeta",
        coldkey="5ColdAlpha",
        sha256=shared_sha,
        created_at=_T0 + timedelta(hours=1),
    )
    await _seed(
        maker,
        name="c1",
        hotkey="5HotGamma",
        coldkey="5ColdOther",
        created_at=_T0 + timedelta(hours=2),
    )
    await _seed(
        maker,
        name="legacy",
        hotkey="5HotAlpha",
        coldkey=None,
        created_at=_T0 + timedelta(hours=3),
    )

    assert _names(await _search(client, miner_hotkey="5HotAlpha")) == [
        "legacy@1",
        "a1@1",
    ]
    assert _names(await _search(client, miner_coldkey="5ColdAlpha")) == [
        "b1@1",
        "a1@1",
    ]
    # SHA-256 is matched case-insensitively against the stored lowercase hex.
    assert _names(await _search(client, artifact_sha256=shared_sha.upper())) == [
        "b1@1",
        "a1@1",
    ]
    both = await _search(client, miner_coldkey="5ColdAlpha", miner_hotkey="5HotAlpha")
    assert both["count"] == 1
    assert both["items"][0]["agent_id"] == str(a1)
    assert both["items"][0]["miner_coldkey"] == "5ColdAlpha"
    assert (
        await _search(client, miner_coldkey="5ColdOther", artifact_sha256=shared_sha)
    )["count"] == 0


@pytest.mark.asyncio
async def test_repeatable_status_and_reason_code_filters(
    client: httpx.AsyncClient, maker: async_sessionmaker[AsyncSession]
) -> None:
    await _seed(maker, name="scored")
    await _seed(
        maker,
        name="banned",
        status=AgentStatus.BANNED,
        created_at=_T0 + timedelta(minutes=1),
    )
    await _seed(
        maker,
        name="docker",
        status=AgentStatus.SCREENING_FAILED,
        reason_code="docker-build",
        created_at=_T0 + timedelta(minutes=2),
    )
    await _seed(
        maker,
        name="policy",
        status=AgentStatus.SCREENING_FAILED,
        reason_code="policy-network-egress",
        created_at=_T0 + timedelta(minutes=3),
    )

    statuses = await _search(client, agent_status=["scored", "banned"])
    assert _names(statuses) == ["banned@1", "scored@1"]
    codes = await _search(
        client, screening_reason_code=["docker-build", "policy-network-egress"]
    )
    assert _names(codes) == ["policy@1", "docker@1"]
    narrowed = await _search(
        client,
        agent_status="screening_failed",
        screening_reason_code="docker-build",
    )
    assert _names(narrowed) == ["docker@1"]


@pytest.mark.asyncio
async def test_submitted_window_is_inclusive_after_and_exclusive_before(
    client: httpx.AsyncClient, maker: async_sessionmaker[AsyncSession]
) -> None:
    for hour in range(4):
        await _seed(maker, name=f"h{hour}", created_at=_T0 + timedelta(hours=hour))

    body = await _search(
        client,
        submitted_after=(_T0 + timedelta(hours=1)).isoformat(),
        submitted_before=(_T0 + timedelta(hours=3)).isoformat(),
    )
    assert _names(body) == ["h2@1", "h1@1"]
    assert body["count"] == 2


@pytest.mark.asyncio
async def test_equal_timestamps_keep_a_stable_agent_id_tiebreak(
    client: httpx.AsyncClient, maker: async_sessionmaker[AsyncSession]
) -> None:
    ids = [await _seed(maker, name=f"tie{index}") for index in range(4)]

    body = await _search(client, agent_name_prefix="tie")
    assert [item["agent_id"] for item in body["items"]] == [
        str(agent_id) for agent_id in sorted(ids, reverse=True)
    ]


@pytest.mark.parametrize(
    "params",
    [
        {"agent_name": ""},
        {"agent_name": "x" * 65},
        {"agent_name_prefix": "x" * 65},
        {"miner_hotkey": "5Hot Alpha"},
        {"miner_coldkey": "5" * 65},
        {"artifact_sha256": "ab" * 31},
        {"artifact_sha256": "zz" * 32},
        {"agent_status": "not-a-status"},
        {"screening_reason_code": "docker build"},
        {"screening_reason_code": [f"code-{index}" for index in range(21)]},
        {"submitted_after": "2026-09-01T12:00:00"},
        {"submitted_after": "not-a-date"},
        {
            "submitted_after": "2026-09-02T00:00:00Z",
            "submitted_before": "2026-09-01T00:00:00Z",
        },
    ],
)
@pytest.mark.usefixtures("maker")
@pytest.mark.asyncio
async def test_invalid_filters_are_rejected_with_422(
    client: httpx.AsyncClient, params: dict[str, _Param]
) -> None:
    response = await client.get(_URL, headers=_HEADERS, params=params)
    assert response.status_code == 422, response.text


@pytest.mark.usefixtures("maker")
@pytest.mark.asyncio
async def test_search_requires_the_admin_token(client: httpx.AsyncClient) -> None:
    response = await client.get(_URL, params={"agent_name": "moonlight_v1"})
    assert response.status_code == 401


@pytest.mark.asyncio
async def test_prefix_and_coldkey_filters_have_a_usable_index(
    maker: async_sessionmaker[AsyncSession],
) -> None:
    async with maker() as session:
        rows = (
            await session.execute(
                text(
                    "SELECT indexname, indexdef FROM pg_indexes WHERE indexname "
                    "IN ('agents_name_pattern_idx', "
                    "'evaluation_payments_miner_coldkey_idx')"
                )
            )
        ).all()
    definitions: dict[str, str] = {str(name): str(sql) for name, sql in rows}
    assert "text_pattern_ops" in definitions["agents_name_pattern_idx"]
    assert "(miner_coldkey)" in definitions["evaluation_payments_miner_coldkey_idx"]
