"""The miner's self-serve read of their own harness diagnostics.

Every denial on this route must be indistinguishable from every other, because
the alternative is a membership oracle over other miners' agent ids. That is
what most of these tests are about.

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

from ditto.api_models.miner_logs import HARNESS_LOGS_MAX_SKEW_SECONDS
from ditto.api_server.endpoints.miner_logs import build_harness_logs_payload
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


def _body(
    *,
    keypair: bittensor.Keypair,
    agent_id,
    requested_at: datetime | None = None,
    claimed_hotkey: str | None = None,
) -> dict:
    requested_at = requested_at or datetime.now(UTC)
    wire = requested_at.astimezone(UTC).isoformat(timespec="microseconds")
    hotkey = claimed_hotkey or keypair.ss58_address
    payload = build_harness_logs_payload(
        hotkey_ss58=hotkey, agent_id=str(agent_id), requested_at=wire
    )
    return {
        "miner_hotkey": hotkey,
        "agent_id": str(agent_id),
        "requested_at": requested_at.isoformat(),
        "signature": keypair.sign(payload).hex(),
    }


async def _fail_ticket(
    maker: async_sessionmaker[AsyncSession],
    agent_id,
    *,
    tail: str | None = _TAIL,
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
        await s.commit()


@pytest.mark.asyncio
class TestMinerReadsTheirOwnHarnessLogs:
    async def test_owner_gets_the_container_log_tail(
        self,
        app: FastAPI,
        client: httpx.AsyncClient,
        session_maker: async_sessionmaker[AsyncSession],
    ) -> None:
        """The whole point: the miner sees why their harness died.

        Before this route the owner of agent 5fdadd33 could learn only
        `scoring_error` -- a reissue-policy class that says nothing about the
        failure. The tail is the first thing on this wire that answers "why".
        """
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

        got = await client.post(
            "/api/v1/miner/harness-logs",
            json=_body(keypair=miner, agent_id=agent_id),
        )

        assert got.status_code == 200, got.text
        body = got.json()
        assert body["miner_hotkey"] == miner.ss58_address
        assert len(body["attempts"]) == 1
        attempt = body["attempts"][0]
        assert attempt["failure_reason"] == "scoring_error"
        assert attempt["container_log_tail"] == _TAIL

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

        got = await client.post(
            "/api/v1/miner/harness-logs",
            json=_body(keypair=miner, agent_id=agent_id),
        )

        assert got.status_code == 200, got.text
        assert got.json()["attempts"][0]["container_log_tail"] is None


@pytest.mark.asyncio
class TestEveryDenialLooksIdentical:
    """A caller must not be able to tell these four cases apart.

    Distinguishing "signature bad" from "not your agent" would confirm that an
    agent id exists and belongs to somebody, which is exactly the probe this
    route must not answer.
    """

    async def _denied(self, client: httpx.AsyncClient, body: dict) -> httpx.Response:
        got = await client.post("/api/v1/miner/harness-logs", json=body)
        assert got.status_code == 404, got.text
        return got

    async def test_another_miners_agent_is_not_readable(
        self,
        app: FastAPI,
        client: httpx.AsyncClient,
        session_maker: async_sessionmaker[AsyncSession],
    ) -> None:
        """The core authorization check: a valid signature over someone else's
        agent proves possession of the WRONG hotkey and must return nothing."""
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

        got = await self._denied(client, _body(keypair=attacker, agent_id=agent_id))
        assert _TAIL not in got.text

    async def test_claiming_the_owners_hotkey_without_their_key_fails(
        self,
        app: FastAPI,
        client: httpx.AsyncClient,
        session_maker: async_sessionmaker[AsyncSession],
    ) -> None:
        """Naming the right hotkey is not the same as holding it."""
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

        got = await self._denied(
            client,
            _body(
                keypair=attacker,
                agent_id=agent_id,
                claimed_hotkey=owner.ss58_address,
            ),
        )
        assert _TAIL not in got.text

    async def test_a_stale_signature_is_refused(
        self,
        app: FastAPI,
        client: httpx.AsyncClient,
        session_maker: async_sessionmaker[AsyncSession],
    ) -> None:
        """A captured signature must stop working once the window passes."""
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

        stale = datetime.now(UTC) - timedelta(
            seconds=HARNESS_LOGS_MAX_SKEW_SECONDS + 60
        )
        await self._denied(
            client, _body(keypair=miner, agent_id=agent_id, requested_at=stale)
        )

    async def test_an_unknown_agent_is_refused(
        self,
        app: FastAPI,
        client: httpx.AsyncClient,
        session_maker: async_sessionmaker[AsyncSession],
    ) -> None:
        _install_db(app, session_maker)
        _install_chain(app)

        await self._denied(client, _body(keypair=_miner(), agent_id=uuid4()))


def test_signed_payload_matches_the_miner_cli_vector() -> None:
    """Pin the exact bytes both sides build.

    The CLI's copy lives in ``ditto.miner_cli.signing`` in the monorepo root and
    cannot be imported here -- the two ``ditto`` packages have different roots --
    so the contract is held by asserting both against the same literal vector.
    The twin lives in
    ``ditto/tests/miner_cli/test_signing.py::test_payload_is_the_versioned_four_field_form``
    and the two must be changed together.
    """
    assert build_harness_logs_payload(
        hotkey_ss58="5GrwvaEF5zXb26Fz9rcQpDWS57CtERHpNehXCPcNoHGKutQY",
        agent_id="5fdadd33-bd0f-492d-ba71-49bef159f069",
        requested_at="2026-08-14T13:27:36.760189+00:00",
    ) == (
        b"ditto-harness-logs:v1:"
        b"5GrwvaEF5zXb26Fz9rcQpDWS57CtERHpNehXCPcNoHGKutQY:"
        b"5fdadd33-bd0f-492d-ba71-49bef159f069:"
        b"2026-08-14T13:27:36.760189+00:00"
    )
