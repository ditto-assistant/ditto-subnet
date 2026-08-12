"""Guardrails for the fleet-wide validator interruption/update control."""

from __future__ import annotations

from collections.abc import AsyncIterator
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4

import httpx
from fastapi import FastAPI
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from ditto.api_models.agent_status import AgentStatus
from ditto.api_models.ticket_status import TicketStatus
from ditto.api_models.validator_fleet_update import CONFIRMATION
from ditto.api_server.dependencies import get_session
from ditto.db.models import (
    Agent,
    ValidatorFleetUpdateOperation,
    ValidatorHeartbeat,
    ValidatorLeaseAudit,
    ValidatorTicket,
)

_ADMIN_TOKEN = "test-admin-token-at-least-32-characters"
_HEADERS = {
    "Authorization": f"Bearer {_ADMIN_TOKEN}",
    "X-Admin-Actor": "operator@example.com",
}
_URL = "/api/v1/admin/validator-fleet-update"
_MANAGED = "managed-validator"


def _install(app: FastAPI, maker: async_sessionmaker[AsyncSession]) -> None:
    app.state.config = replace(app.state.config, admin_api_token=_ADMIN_TOKEN)
    app.state.session_maker = maker

    async def _session() -> AsyncIterator[AsyncSession]:
        async with maker() as session:
            yield session

    app.dependency_overrides[get_session] = _session


async def _seed(maker: async_sessionmaker[AsyncSession]) -> UUID:
    now = datetime.now(UTC)
    agent_id = uuid4()
    async with maker() as session, session.begin():
        session.add(
            Agent(
                agent_id=agent_id,
                miner_hotkey="fleet-update-miner",
                name="fleet-update-agent",
                sha256="a" * 64,
                status=AgentStatus.EVALUATING,
            )
        )
        session.add_all(
            [
                ValidatorHeartbeat(
                    validator_hotkey=_MANAGED,
                    software_version="0.53.14",
                    protocol_version=19,
                    code_digest="b" * 64,
                    state="running_benchmark",
                    first_seen_at=now,
                    reported_at=now,
                    seen_at=now,
                    signature="ab" * 64,
                    capabilities={"stack_updater": True},
                    stack={
                        "mode": "managed",
                        "components": {"ditto_subnet": {"source_revision": "c" * 40}},
                    },
                ),
                ValidatorHeartbeat(
                    validator_hotkey="self-managed-validator",
                    software_version="0.53.14",
                    protocol_version=19,
                    code_digest="d" * 64,
                    state="running_benchmark",
                    first_seen_at=now,
                    reported_at=now,
                    seen_at=now,
                    signature="cd" * 64,
                    capabilities={"stack_updater": False},
                    stack={"mode": "self_managed", "components": {}},
                ),
            ]
        )
        session.add(
            ValidatorTicket(
                agent_id=agent_id,
                validator_hotkey=_MANAGED,
                slot_id="slot-0",
                bench_version=8,
                status=TicketStatus.ISSUED,
                issued_at=now - timedelta(minutes=3),
                deadline=now + timedelta(minutes=87),
                attempt_count=1,
            )
        )
    return agent_id


async def test_preview_and_force_update_are_snapshot_bound_and_audited(
    app: FastAPI,
    client: httpx.AsyncClient,
    session_maker: async_sessionmaker[AsyncSession],
) -> None:
    _install(app, session_maker)
    agent_id = await _seed(session_maker)

    preview = await client.get(_URL, headers=_HEADERS)
    assert preview.status_code == 200, preview.text
    before = preview.json()
    assert before["target_count"] == 1
    assert before["active_lease_count"] == 1
    assert before["targets"][0]["validator_hotkey"] == _MANAGED
    assert before["targets"][0]["stack_revision"] == "c" * 40

    request_id = uuid4()
    response = await client.post(
        _URL,
        headers=_HEADERS,
        json={
            "request_id": str(request_id),
            "expected_snapshot": before["snapshot"],
            "reason": "emergency scorer repair across the managed fleet",
            "actor": "ignored-body-actor",
            "confirmation": CONFIRMATION,
        },
    )
    assert response.status_code == 200, response.text
    operation = response.json()["operation"]
    assert operation["operation_id"] == str(request_id)
    assert operation["revoked_lease_count"] == 1
    assert operation["actor"] == "operator@example.com"
    assert operation["acknowledged_count"] == 0

    async with session_maker() as session:
        ticket = await session.get(ValidatorTicket, (agent_id, 8, _MANAGED))
        row = await session.get(ValidatorFleetUpdateOperation, request_id)
        audits = list(
            await session.scalars(
                select(ValidatorLeaseAudit).where(
                    ValidatorLeaseAudit.agent_id == agent_id,
                    ValidatorLeaseAudit.action == "operator_forced_update",
                )
            )
        )
    assert ticket is not None and ticket.status == TicketStatus.EXPIRED
    assert row is not None and row.target_validator_hotkeys == [_MANAGED]
    assert len(audits) == 1
    assert audits[0].context == "admin_fleet_update"

    replay = await client.post(
        _URL,
        headers=_HEADERS,
        json={
            "request_id": str(request_id),
            "expected_snapshot": before["snapshot"],
            "reason": "emergency scorer repair across the managed fleet",
            "actor": "ignored-body-actor",
            "confirmation": CONFIRMATION,
        },
    )
    assert replay.status_code == 200, replay.text
    assert replay.json()["idempotent"] is True


async def test_force_update_refuses_stale_snapshot_and_wrong_confirmation(
    app: FastAPI,
    client: httpx.AsyncClient,
    session_maker: async_sessionmaker[AsyncSession],
) -> None:
    _install(app, session_maker)
    await _seed(session_maker)
    preview = (await client.get(_URL, headers=_HEADERS)).json()
    base = {
        "request_id": str(uuid4()),
        "expected_snapshot": "0" * 64,
        "reason": "emergency scorer repair across the managed fleet",
        "actor": "operator@example.com",
        "confirmation": CONFIRMATION,
    }
    stale = await client.post(_URL, headers=_HEADERS, json=base)
    assert stale.status_code == 409
    assert "refresh" in stale.json()["message"]

    wrong = await client.post(
        _URL,
        headers=_HEADERS,
        json={
            **base,
            "request_id": str(uuid4()),
            "expected_snapshot": preview["snapshot"],
            "confirmation": "UPDATE THEM",
        },
    )
    assert wrong.status_code == 409


async def test_preview_requires_admin_token(
    app: FastAPI,
    client: httpx.AsyncClient,
    session_maker: async_sessionmaker[AsyncSession],
) -> None:
    _install(app, session_maker)
    assert (await client.get(_URL)).status_code == 401
