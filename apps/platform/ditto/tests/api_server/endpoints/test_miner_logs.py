"""The signed-in miner's read of their own harness diagnostics.

Every denial that is not a missing session must look like every other 404,
because distinguishing "not your agent" from "no such agent" would be a
membership oracle over other miners' agent ids.

Seeding helpers are imported from ``test_validator`` rather than duplicated: the
fixtures encode a lot of era-specific knowledge (screened images, dataset
versions, ticket floors) that has to stay in one place to stay correct.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from uuid import uuid4

import bittensor
import httpx
import pytest
from fastapi import FastAPI
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from ditto.api_server.miner_session import login_message
from ditto.db.models import AgentStatus, ValidatorTicket
from ditto.tests.api_server.endpoints.test_validator import (
    _BENCH_VERSION,
    _VALIDATOR_HOTKEY,
    _install_chain,
    _install_db,
    _seed_agent,
    _seed_ticket,
)

_TAIL = "thread 'main' panicked at src/main.rs:42: cannot open /data/index"


def _miner() -> bittensor.Keypair:
    return bittensor.Keypair.create_from_uri("//Alice")


async def _login(
    client: httpx.AsyncClient,
    *,
    keypair: bittensor.Keypair,
    scopes: list[str] | None = None,
) -> str:
    started = await client.post(
        "/api/v1/miner-auth/device",
        json={"scopes": scopes or ["read"], "ttl_seconds": 3600},
    )
    assert started.status_code == 200, started.text
    user_code = started.json()["user_code"]
    public = await client.get(f"/api/v1/miner-auth/device/{user_code}")
    grant_id = public.json()["grant_id"]
    nonce = uuid4()
    issued_at = datetime.now(UTC)
    requested = scopes or ["read"]
    payload = login_message(
        netuid=118,
        miner_hotkey=keypair.ss58_address,
        user_code=user_code,
        grant_id=grant_id,
        ttl_seconds=3600,
        scopes=",".join(sorted(requested)),
        nonce=nonce,
        issued_at=issued_at,
        key_kind="hotkey",
        signer=keypair.ss58_address,
    )
    approved = await client.post(
        f"/api/v1/miner-auth/device/{user_code}/approve",
        json={
            "netuid": 118,
            "miner_hotkey": keypair.ss58_address,
            "nonce": str(nonce),
            "issued_at": issued_at.astimezone(UTC).isoformat(timespec="microseconds"),
            "proof": {
                "key_kind": "hotkey",
                "signer": keypair.ss58_address,
                "signature": keypair.sign(payload).hex(),
            },
        },
    )
    assert approved.status_code == 200, approved.text
    token = approved.json()["access_token"]
    assert isinstance(token, str)
    return token


async def _fail_ticket(
    maker: async_sessionmaker[AsyncSession],
    agent_id,
    *,
    tail: str | None = _TAIL,
    attempt: int = 1,
) -> None:
    """Put the ticket in the exact shape agent 5fdadd33 left behind."""
    async with maker() as s:
        ticket = await s.get(
            ValidatorTicket, (agent_id, _BENCH_VERSION, _VALIDATOR_HOTKEY)
        )
        assert ticket is not None
        ticket.failure_reason = "scoring_error"
        ticket.failed_at = datetime.now(UTC)
        ticket.container_log_tail = tail
        ticket.container_log_tail_attempt = attempt if tail is not None else None
        ticket.attempt_count = attempt
        await s.commit()


@pytest.mark.asyncio
class TestMinerReadsTheirOwnHarnessLogs:
    async def test_owner_gets_the_container_log_tail(
        self,
        app: FastAPI,
        client: httpx.AsyncClient,
        session_maker: async_sessionmaker[AsyncSession],
    ) -> None:
        """The whole point: the signed-in miner sees why their harness died."""
        miner = _miner()
        agent_id = await _seed_agent(
            session_maker,
            status=AgentStatus.EVALUATING,
            miner_hotkey=miner.ss58_address,
        )
        await _seed_ticket(session_maker, agent_id)
        await _fail_ticket(session_maker, agent_id)
        _install_db(app, session_maker)
        _install_chain(app)
        token = await _login(client, keypair=miner)

        got = await client.get(
            f"/api/v1/me/agents/{agent_id}/harness-logs",
            headers={"authorization": f"Bearer {token}"},
        )

        assert got.status_code == 200, got.text
        body = got.json()
        assert body["miner_hotkey"] == miner.ss58_address
        assert len(body["attempts"]) == 1
        attempt = body["attempts"][0]
        assert attempt["failure_reason"] == "scoring_error"
        assert attempt["container_log_tail"] == _TAIL
        assert attempt["attempt_count"] == 1
        assert attempt["log_tail_attempt"] == 1
        assert attempt["stale"] is False

    async def test_reissued_ticket_marks_the_old_tail_stale(
        self,
        app: FastAPI,
        client: httpx.AsyncClient,
        session_maker: async_sessionmaker[AsyncSession],
    ) -> None:
        """Reissue restamps issued_at; the tail still belongs to attempt 1."""
        miner = _miner()
        agent_id = await _seed_agent(
            session_maker,
            status=AgentStatus.EVALUATING,
            miner_hotkey=miner.ss58_address,
        )
        await _seed_ticket(session_maker, agent_id)
        await _fail_ticket(session_maker, agent_id, attempt=1)
        async with session_maker() as s:
            ticket = await s.get(
                ValidatorTicket, (agent_id, _BENCH_VERSION, _VALIDATOR_HOTKEY)
            )
            assert ticket is not None
            ticket.attempt_count = 2
            ticket.issued_at = datetime.now(UTC) + timedelta(seconds=30)
            await s.commit()
        _install_db(app, session_maker)
        _install_chain(app)
        token = await _login(client, keypair=miner)

        got = await client.get(
            f"/api/v1/me/agents/{agent_id}/harness-logs",
            headers={"authorization": f"Bearer {token}"},
        )

        assert got.status_code == 200, got.text
        attempt = got.json()["attempts"][0]
        assert attempt["container_log_tail"] == _TAIL
        assert attempt["attempt_count"] == 2
        assert attempt["log_tail_attempt"] == 1
        assert attempt["stale"] is True

    async def test_a_run_with_no_tail_reports_null_not_an_empty_string(
        self,
        app: FastAPI,
        client: httpx.AsyncClient,
        session_maker: async_sessionmaker[AsyncSession],
    ) -> None:
        """Absent evidence must not read as "the harness printed nothing"."""
        miner = _miner()
        agent_id = await _seed_agent(
            session_maker,
            status=AgentStatus.EVALUATING,
            miner_hotkey=miner.ss58_address,
        )
        await _seed_ticket(session_maker, agent_id)
        await _fail_ticket(session_maker, agent_id, tail=None)
        _install_db(app, session_maker)
        _install_chain(app)
        token = await _login(client, keypair=miner)

        got = await client.get(
            f"/api/v1/me/agents/{agent_id}/harness-logs",
            headers={"authorization": f"Bearer {token}"},
        )

        assert got.status_code == 200, got.text
        attempt = got.json()["attempts"][0]
        assert attempt["container_log_tail"] is None
        assert attempt["stale"] is False


@pytest.mark.asyncio
class TestEveryDenialLooksIdentical:
    async def test_another_miners_agent_is_not_readable(
        self,
        app: FastAPI,
        client: httpx.AsyncClient,
        session_maker: async_sessionmaker[AsyncSession],
    ) -> None:
        owner, attacker = _miner(), bittensor.Keypair.create_from_uri("//Bob")
        agent_id = await _seed_agent(
            session_maker,
            status=AgentStatus.EVALUATING,
            miner_hotkey=owner.ss58_address,
        )
        await _seed_ticket(session_maker, agent_id)
        await _fail_ticket(session_maker, agent_id)
        _install_db(app, session_maker)
        _install_chain(app)
        token = await _login(client, keypair=attacker)

        got = await client.get(
            f"/api/v1/me/agents/{agent_id}/harness-logs",
            headers={"authorization": f"Bearer {token}"},
        )
        assert got.status_code == 404, got.text
        assert _TAIL not in got.text

    async def test_missing_session_is_401(
        self,
        app: FastAPI,
        client: httpx.AsyncClient,
        session_maker: async_sessionmaker[AsyncSession],
    ) -> None:
        _install_db(app, session_maker)
        _install_chain(app)
        got = await client.get(f"/api/v1/me/agents/{uuid4()}/harness-logs")
        assert got.status_code == 401

    async def test_an_unknown_agent_is_refused(
        self,
        app: FastAPI,
        client: httpx.AsyncClient,
        session_maker: async_sessionmaker[AsyncSession],
    ) -> None:
        _install_db(app, session_maker)
        _install_chain(app)
        token = await _login(client, keypair=_miner())
        got = await client.get(
            f"/api/v1/me/agents/{uuid4()}/harness-logs",
            headers={"authorization": f"Bearer {token}"},
        )
        assert got.status_code == 404
