"""Payment-derived owner-footprint API coverage.

Also asserts the coldkey now surfaced on the screening-submission and
quarantine-context reads, since those share the same "one signal, not proof"
contract and regress the same way.
"""

from collections.abc import AsyncIterator
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4

import httpx
import pytest
from fastapi import FastAPI
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from ditto.api_server.dependencies import get_session
from ditto.db.models import (
    Agent,
    AgentStatus,
    EvaluationPayment,
    ScreeningAttempt,
    ScreeningQuarantine,
)

_TOKEN = "test-admin-token-at-least-32-characters"
_HEADERS = {"Authorization": f"Bearer {_TOKEN}", "X-Admin-Actor": "operator"}
_T0 = datetime(2026, 7, 20, 12, tzinfo=UTC)


@pytest.fixture
def maker(
    session_maker: async_sessionmaker[AsyncSession],
) -> async_sessionmaker[AsyncSession]:
    return session_maker


def _install(app: FastAPI, maker: async_sessionmaker[AsyncSession]) -> None:
    app.state.config = replace(app.state.config, admin_api_token=_TOKEN)

    async def _session() -> AsyncIterator[AsyncSession]:
        async with maker() as session:
            yield session

    app.dependency_overrides[get_session] = _session


async def _seed_agent(
    maker: async_sessionmaker[AsyncSession],
    *,
    miner_hotkey: str,
    miner_coldkey: str | None,
    name: str = "agent",
    created_at: datetime = _T0,
    status: AgentStatus = AgentStatus.SCORED,
) -> UUID:
    agent_id = uuid4()
    async with maker() as session, session.begin():
        session.add(
            Agent(
                agent_id=agent_id,
                miner_hotkey=miner_hotkey,
                name=name,
                sha256=agent_id.hex * 2,
                status=status,
                created_at=created_at,
                screening_policy_version=8,
            )
        )
        if miner_coldkey is not None:
            await session.flush()
            session.add(
                EvaluationPayment(
                    block_hash=f"0x{agent_id.hex}",
                    extrinsic_index=0,
                    agent_id=agent_id,
                    miner_hotkey=miner_hotkey,
                    miner_coldkey=miner_coldkey,
                    amount_rao=1,
                    dest_address="5Destination",
                    timestamp=created_at,
                )
            )
    return agent_id


async def _footprint(client: httpx.AsyncClient, identifier: str, **params: int) -> dict:
    response = await client.get(
        f"/api/v1/admin/miner-owners/{identifier}",
        headers=_HEADERS,
        params=params,
    )
    assert response.status_code == 200, response.text
    return response.json()


@pytest.mark.asyncio
async def test_hotkey_resolves_every_sibling_hotkey_of_its_payment_coldkey(
    app: FastAPI, client: httpx.AsyncClient, maker: async_sessionmaker[AsyncSession]
) -> None:
    _install(app, maker)
    await _seed_agent(maker, miner_hotkey="5Alpha", miner_coldkey="5Cold", name="a1")
    await _seed_agent(
        maker,
        miner_hotkey="5Alpha",
        miner_coldkey="5Cold",
        name="a2",
        created_at=_T0 + timedelta(hours=1),
    )
    await _seed_agent(maker, miner_hotkey="5Beta", miner_coldkey="5Cold", name="b1")
    # Paid from an unrelated coldkey: must not appear in the footprint.
    await _seed_agent(maker, miner_hotkey="5Gamma", miner_coldkey="5Other", name="g1")

    body = await _footprint(client, "5Alpha")

    assert body["identifier_kind"] == "miner_hotkey"
    assert body["ownership_basis"] == "evaluation_payment_records"
    assert body["miner_coldkeys"] == ["5Cold"]
    assert [entry["miner_hotkey"] for entry in body["hotkeys"]] == ["5Alpha", "5Beta"]
    assert body["hotkey_count"] == 2
    assert body["submission_count"] == 3
    assert body["expansion_complete"] is True

    alpha, beta = body["hotkeys"]
    assert alpha["link_hop"] == 0
    assert beta["link_hop"] == 1
    assert alpha["submission_count"] == 2
    assert alpha["paid_submission_count"] == 2
    assert alpha["latest_submitted_at"].startswith("2026-07-20T13:00")
    # Newest first, so a reviewer reads the current generation at the top.
    assert [agent["agent_name"] for agent in alpha["agents"]] == ["a2", "a1"]
    assert alpha["agents_truncated"] is False
    assert {agent["miner_coldkey"] for agent in alpha["agents"]} == {"5Cold"}


@pytest.mark.asyncio
async def test_coldkey_identifier_resolves_its_hotkeys(
    app: FastAPI, client: httpx.AsyncClient, maker: async_sessionmaker[AsyncSession]
) -> None:
    _install(app, maker)
    await _seed_agent(maker, miner_hotkey="5Alpha", miner_coldkey="5Cold")
    await _seed_agent(maker, miner_hotkey="5Beta", miner_coldkey="5Cold")

    body = await _footprint(client, "5Cold")

    assert body["identifier_kind"] == "miner_coldkey"
    assert [entry["miner_hotkey"] for entry in body["hotkeys"]] == ["5Alpha", "5Beta"]
    assert {entry["link_hop"] for entry in body["hotkeys"]} == {1}


@pytest.mark.asyncio
async def test_depth_bounds_transitive_linkage_and_reports_incompleteness(
    app: FastAPI, client: httpx.AsyncClient, maker: async_sessionmaker[AsyncSession]
) -> None:
    """A -> cold1 <- B -> cold2 <- C is a real chain, but a weak one.

    Depth 1 must stop at B and say the walk is not complete, rather than
    silently presenting C as part of the same operator.
    """
    _install(app, maker)
    await _seed_agent(maker, miner_hotkey="5A", miner_coldkey="5Cold1")
    await _seed_agent(maker, miner_hotkey="5B", miner_coldkey="5Cold1")
    await _seed_agent(maker, miner_hotkey="5B", miner_coldkey="5Cold2", name="b2")
    await _seed_agent(maker, miner_hotkey="5C", miner_coldkey="5Cold2")

    shallow = await _footprint(client, "5A", depth=1)
    assert [entry["miner_hotkey"] for entry in shallow["hotkeys"]] == ["5A", "5B"]
    assert shallow["expansion_complete"] is False

    deep = await _footprint(client, "5A", depth=2)
    assert [entry["miner_hotkey"] for entry in deep["hotkeys"]] == ["5A", "5B", "5C"]
    assert deep["expansion_complete"] is True
    hops = {entry["miner_hotkey"]: entry["link_hop"] for entry in deep["hotkeys"]}
    # C is two payment hops out: weaker evidence than B, and labelled as such.
    assert hops == {"5A": 0, "5B": 1, "5C": 3}


@pytest.mark.asyncio
async def test_unpaid_hotkey_is_reported_without_inventing_a_coldkey(
    app: FastAPI, client: httpx.AsyncClient, maker: async_sessionmaker[AsyncSession]
) -> None:
    _install(app, maker)
    await _seed_agent(maker, miner_hotkey="5Legacy", miner_coldkey=None)

    body = await _footprint(client, "5Legacy")

    assert body["identifier_kind"] == "miner_hotkey"
    assert body["miner_coldkeys"] == []
    assert body["expansion_complete"] is True
    (entry,) = body["hotkeys"]
    assert entry["miner_coldkeys"] == []
    assert entry["submission_count"] == 1
    # The gap between total and paid is the part no coldkey can speak to.
    assert entry["paid_submission_count"] == 0
    assert entry["agents"][0]["miner_coldkey"] is None


@pytest.mark.asyncio
async def test_unknown_key_answers_empty_rather_than_404(
    app: FastAPI, client: httpx.AsyncClient, maker: async_sessionmaker[AsyncSession]
) -> None:
    """ "No record" is an answer a reviewer needs, not an error."""
    _install(app, maker)

    body = await _footprint(client, "5NeverSeen")

    assert body["identifier_kind"] == "unknown"
    assert body["hotkeys"] == []
    assert body["hotkey_count"] == 0
    assert body["submission_count"] == 0
    assert body["expansion_complete"] is True
    assert "not on-chain metagraph ownership" in body["linkage_caveat"]


@pytest.mark.asyncio
async def test_agents_per_hotkey_bounds_the_sample_and_flags_truncation(
    app: FastAPI, client: httpx.AsyncClient, maker: async_sessionmaker[AsyncSession]
) -> None:
    _install(app, maker)
    for index in range(3):
        await _seed_agent(
            maker,
            miner_hotkey="5Prolific",
            miner_coldkey="5Cold",
            name=f"gen{index}",
            created_at=_T0 + timedelta(hours=index),
        )

    body = await _footprint(client, "5Prolific", agents_per_hotkey=2)

    (entry,) = body["hotkeys"]
    assert entry["submission_count"] == 3
    assert [agent["agent_name"] for agent in entry["agents"]] == ["gen2", "gen1"]
    assert entry["agents_truncated"] is True


@pytest.mark.asyncio
async def test_footprint_requires_the_admin_token(
    app: FastAPI, client: httpx.AsyncClient, maker: async_sessionmaker[AsyncSession]
) -> None:
    _install(app, maker)
    response = await client.get("/api/v1/admin/miner-owners/5Alpha")
    assert response.status_code == 401


@pytest.mark.asyncio
async def test_screening_submission_reads_expose_the_payment_coldkey(
    app: FastAPI, client: httpx.AsyncClient, maker: async_sessionmaker[AsyncSession]
) -> None:
    _install(app, maker)
    paid = await _seed_agent(maker, miner_hotkey="5Alpha", miner_coldkey="5Cold")
    unpaid = await _seed_agent(maker, miner_hotkey="5Legacy", miner_coldkey=None)

    detail = await client.get(
        f"/api/v1/admin/screening-submissions/{paid}", headers=_HEADERS
    )
    assert detail.status_code == 200, detail.text
    assert detail.json()["miner_coldkey"] == "5Cold"

    legacy = await client.get(
        f"/api/v1/admin/screening-submissions/{unpaid}", headers=_HEADERS
    )
    assert legacy.status_code == 200, legacy.text
    assert legacy.json()["miner_coldkey"] is None

    listing = await client.get("/api/v1/admin/screening-submissions", headers=_HEADERS)
    assert listing.status_code == 200, listing.text
    by_agent = {item["agent_id"]: item for item in listing.json()["items"]}
    assert by_agent[str(paid)]["miner_coldkey"] == "5Cold"
    assert by_agent[str(unpaid)]["miner_coldkey"] is None


@pytest.mark.asyncio
async def test_quarantine_context_names_the_coldkey_behind_same_owner(
    app: FastAPI, client: httpx.AsyncClient, maker: async_sessionmaker[AsyncSession]
) -> None:
    """The duplicate-adjudication case: show the keys, not just a boolean."""
    _install(app, maker)
    held = await _seed_agent(
        maker,
        miner_hotkey="5Alpha",
        miner_coldkey="5Cold",
        name="held",
        status=AgentStatus.SCREENING,
    )
    async with maker() as session, session.begin():
        agent = await session.get(Agent, held)
        assert agent is not None
        sha = agent.sha256
    # Same artifact, different hotkey, same payer: same operator.
    sibling = await _seed_agent(
        maker, miner_hotkey="5Beta", miner_coldkey="5Cold", name="sibling"
    )
    # Same artifact, different hotkey, different payer: a third party.
    stranger = await _seed_agent(
        maker, miner_hotkey="5Gamma", miner_coldkey="5Other", name="stranger"
    )
    quarantine_id = uuid4()
    async with maker() as session, session.begin():
        for agent_id in (sibling, stranger):
            other = await session.get(Agent, agent_id)
            assert other is not None
            other.sha256 = sha
        attempt_id = uuid4()
        session.add(
            ScreeningAttempt(
                attempt_id=attempt_id,
                agent_id=held,
                policy_version=8,
                status="quarantined",
                screener_hotkey="5Screener",
                started_at=_T0,
                deadline=_T0 + timedelta(minutes=30),
            )
        )
        await session.flush()
        session.add(
            ScreeningQuarantine(
                quarantine_id=quarantine_id,
                agent_id=held,
                attempt_id=attempt_id,
                screener_hotkey="5Screener",
                policy_version=8,
                manifest_digest="c" * 64,
                reason_code="duplicate-artifact",
                status="active",
                created_at=_T0,
            )
        )

    response = await client.get(
        f"/api/v1/admin/screening-quarantines/{quarantine_id}/context",
        headers=_HEADERS,
    )
    assert response.status_code == 200, response.text
    body = response.json()

    assert body["agent"]["miner_coldkey"] == "5Cold"
    assert body["quarantine"]["miner_coldkey"] == "5Cold"
    assert body["miner"]["miner_coldkeys"] == ["5Cold"]
    coldkeys = {
        duplicate["miner_hotkey"]: duplicate["miner_coldkey"]
        for duplicate in body["duplicates"]
    }
    assert coldkeys == {"5Beta": "5Cold", "5Gamma": "5Other"}
    same_owner = {
        duplicate["miner_hotkey"]: duplicate["same_owner"]
        for duplicate in body["duplicates"]
    }
    assert same_owner == {"5Beta": True, "5Gamma": False}
