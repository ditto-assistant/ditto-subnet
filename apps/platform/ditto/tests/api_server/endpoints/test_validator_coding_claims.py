from __future__ import annotations

from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from typing import cast
from unittest.mock import AsyncMock
from uuid import UUID, uuid4

import bittensor
import httpx
from fastapi import FastAPI
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from ditto.api_models.coding_claims import (
    CodingClaimActionRequest,
    CodingClaimNextRequest,
    coding_claim_action_signing_message,
    coding_claim_next_signing_message,
)
from ditto.api_server.dependencies import get_chain_client, get_session
from ditto.api_server.endpoints import validator_coding_claims as endpoint_module
from ditto.db.models import CodingShadowRun, CodingShadowTicket
from ditto.db.queries.coding_claims import CodingTicketClaim
from ditto.db.queries.validator_auth import ValidatorRequestReplayError

_KEYPAIR = bittensor.Keypair.create_from_uri("//Alice")
_VALIDATOR = _KEYPAIR.ss58_address
_TICKET = UUID("33333333-3333-4333-8333-333333333333")
_RUN = UUID("bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb")
_AGENT = UUID("aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa")
_INSTANCE = "coding-worker-instance-001"


def _result(*, started: bool = False, idempotent: bool = False) -> CodingTicketClaim:
    now = datetime.now(UTC)
    ticket = SimpleNamespace(
        ticket_id=_TICKET,
        run_row_id=_RUN,
        validator_hotkey=_VALIDATOR,
        deadline=now + timedelta(hours=1),
        claim_generation=1,
        claim_instance_id=_INSTANCE,
        claim_expires_at=now + timedelta(minutes=2),
        claim_started_at=now if started else None,
    )
    run = SimpleNamespace(
        agent_id=_AGENT,
        run_row_id=_RUN,
        bench_version=12,
        coding_run_id="coding-run-001",
        artifact_sha256="aa" * 32,
        screened_image_sha256="bb" * 32,
        run_manifest_sha256="cc" * 32,
        task_set_manifest_sha256="dd" * 32,
    )
    return CodingTicketClaim(
        ticket=cast(CodingShadowTicket, ticket),
        run=cast(CodingShadowRun, run),
        instance_id=_INSTANCE,
        idempotent=idempotent,
    )


def _next_payload(**updates) -> dict:
    nonce = updates.pop("nonce", uuid4())
    requested_at = updates.pop("requested_at", datetime.now(UTC))
    request = CodingClaimNextRequest(
        validator_hotkey=_VALIDATOR,
        instance_id=_INSTANCE,
        run_row_id=_RUN,
        nonce=nonce,
        requested_at=requested_at,
        signature=_KEYPAIR.sign(
            coding_claim_next_signing_message(
                validator_hotkey=_VALIDATOR,
                instance_id=_INSTANCE,
                run_row_id=_RUN,
                nonce=nonce,
                requested_at=requested_at,
            )
        ).hex(),
    ).model_dump(mode="json")
    request.update(updates)
    return request


def _action_payload(action: str, **updates) -> dict:
    nonce = updates.pop("nonce", uuid4())
    requested_at = updates.pop("requested_at", datetime.now(UTC))
    request = CodingClaimActionRequest(
        validator_hotkey=_VALIDATOR,
        instance_id=_INSTANCE,
        run_row_id=_RUN,
        ticket_id=_TICKET,
        claim_generation=1,
        nonce=nonce,
        requested_at=requested_at,
        signature=_KEYPAIR.sign(
            coding_claim_action_signing_message(
                action=action,  # type: ignore[arg-type]
                validator_hotkey=_VALIDATOR,
                instance_id=_INSTANCE,
                run_row_id=_RUN,
                ticket_id=_TICKET,
                claim_generation=1,
                nonce=nonce,
                requested_at=requested_at,
            )
        ).hex(),
    ).model_dump(mode="json")
    request.update(updates)
    return request


def _install(
    app: FastAPI,
    maker: async_sessionmaker[AsyncSession],
    monkeypatch,
) -> SimpleNamespace:
    async def _session() -> AsyncIterator[AsyncSession]:
        async with maker() as session:
            yield session

    async def _chain():
        return object()

    app.dependency_overrides[get_session] = _session
    app.dependency_overrides[get_chain_client] = _chain
    mocks = SimpleNamespace(
        consume=AsyncMock(return_value=None),
        claim=AsyncMock(return_value=_result()),
        start=AsyncMock(return_value=_result(started=True)),
        heartbeat=AsyncMock(return_value=_result(started=True)),
    )
    monkeypatch.setattr(
        endpoint_module,
        "_assert_validator_permitted",
        AsyncMock(return_value=None),
    )
    monkeypatch.setattr(endpoint_module, "consume_validator_nonce", mocks.consume)
    monkeypatch.setattr(endpoint_module, "claim_next_coding_ticket", mocks.claim)
    monkeypatch.setattr(endpoint_module, "start_coding_ticket_claim", mocks.start)
    monkeypatch.setattr(
        endpoint_module,
        "heartbeat_coding_ticket_claim",
        mocks.heartbeat,
    )
    return mocks


async def test_claim_next_start_and_heartbeat_are_signed_and_no_store(
    app: FastAPI,
    client: httpx.AsyncClient,
    session_maker: async_sessionmaker[AsyncSession],
    monkeypatch,
) -> None:
    mocks = _install(app, session_maker, monkeypatch)
    claimed = await client.post(
        "/api/v1/validator/coding-shadow/claims/next",
        json=_next_payload(),
    )
    assert claimed.status_code == 200, claimed.text
    assert claimed.headers["Cache-Control"] == "no-store"
    assert claimed.json()["ticket_id"] == str(_TICKET)
    assert claimed.json()["weight_eligible"] is False

    started = await client.post(
        f"/api/v1/validator/coding-shadow/claims/{_TICKET}/start",
        json=_action_payload("start"),
    )
    assert started.status_code == 200, started.text
    assert started.json()["claim_started_at"] is not None
    heartbeat = await client.post(
        f"/api/v1/validator/coding-shadow/claims/{_TICKET}/heartbeat",
        json=_action_payload("heartbeat"),
    )
    assert heartbeat.status_code == 200, heartbeat.text
    assert mocks.claim.await_count == 1
    assert mocks.start.await_count == 1
    assert mocks.heartbeat.await_count == 1
    assert mocks.consume.await_count == 3
    assert mocks.claim.await_args.kwargs["run_row_id"] == _RUN
    assert mocks.start.await_args.kwargs["run_row_id"] == _RUN
    assert mocks.heartbeat.await_args.kwargs["run_row_id"] == _RUN


async def test_claim_endpoints_reject_forgery_replay_mismatch_and_empty_queue(
    app: FastAPI,
    client: httpx.AsyncClient,
    session_maker: async_sessionmaker[AsyncSession],
    monkeypatch,
) -> None:
    mocks = _install(app, session_maker, monkeypatch)
    forged = _next_payload(signature="00" * 64)
    assert (
        await client.post(
            "/api/v1/validator/coding-shadow/claims/next",
            json=forged,
        )
    ).status_code == 401
    run_forgery = _next_payload(run_row_id=str(uuid4()))
    assert (
        await client.post(
            "/api/v1/validator/coding-shadow/claims/next",
            json=run_forgery,
        )
    ).status_code == 401
    mocks.consume.side_effect = ValidatorRequestReplayError("replay")
    assert (
        await client.post(
            "/api/v1/validator/coding-shadow/claims/next",
            json=_next_payload(),
        )
    ).status_code == 409
    mocks.consume.side_effect = None
    mismatch = await client.post(
        f"/api/v1/validator/coding-shadow/claims/{uuid4()}/start",
        json=_action_payload("start"),
    )
    assert mismatch.status_code == 409
    mocks.claim.return_value = None
    empty = await client.post(
        "/api/v1/validator/coding-shadow/claims/next",
        json=_next_payload(),
    )
    assert empty.status_code == 404
    assert empty.headers["Cache-Control"] == "no-store"
