from __future__ import annotations

from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock
from uuid import UUID, uuid4

import bittensor
import httpx
from fastapi import FastAPI
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from ditto.api_models.coding_harness import (
    CodingHarnessLaunchRequest,
    coding_harness_launch_signing_message,
)
from ditto.api_server.dependencies import get_chain_client, get_session
from ditto.api_server.endpoints import validator_coding_harness as endpoint_module
from ditto.db.queries.coding_task_leases import (
    CodingTaskLeaseNotAvailableError,
)
from ditto.db.queries.validator_auth import ValidatorRequestReplayError
from ditto.tests.api_server.conftest import override_get_storage_client

_KEYPAIR = bittensor.Keypair.create_from_uri("//Alice")
_VALIDATOR = _KEYPAIR.ss58_address
_NOW = datetime.now(UTC).replace(microsecond=0)
_TICKET_ID = UUID("33333333-3333-4333-8333-333333333333")
_AGENT_ID = UUID("aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa")
_RUN_ROW_ID = UUID("bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb")


def _payload(**updates) -> dict:
    nonce = updates.pop("nonce", uuid4())
    requested_at = updates.pop("requested_at", datetime.now(UTC))
    value = CodingHarnessLaunchRequest(
        validator_hotkey=_VALIDATOR,
        ticket_id=_TICKET_ID,
        nonce=nonce,
        requested_at=requested_at,
        signature=_KEYPAIR.sign(
            coding_harness_launch_signing_message(
                validator_hotkey=_VALIDATOR,
                ticket_id=_TICKET_ID,
                nonce=nonce,
                requested_at=requested_at,
            )
        ).hex(),
    ).model_dump(mode="json")
    value.update(updates)
    return value


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
    storage = override_get_storage_client(app)
    storage.presigned_get_url = AsyncMock(
        return_value="https://storage.invalid/screened.tar?signature=test"
    )
    authority = SimpleNamespace(
        agent_id=_AGENT_ID,
        run_row_id=_RUN_ROW_ID,
        ticket_id=_TICKET_ID,
        deadline=_NOW + timedelta(hours=1),
        bench_version=12,
        agent_artifact_sha256="55" * 32,
        screened_image_sha256="66" * 32,
        screened_image_size_bytes=1024,
        screened_image_id="sha256:" + "77" * 32,
        screened_image_ref=f"ditto-screen/{_AGENT_ID}:latest",
        screened_image_upload_id=UUID("cccccccc-cccc-4ccc-8ccc-cccccccccccc"),
        screening_policy_version=9,
    )
    mocks = SimpleNamespace(
        storage=storage,
        authority=authority,
        consume_nonce=AsyncMock(return_value=None),
        authorize=AsyncMock(return_value=authority),
        audit=AsyncMock(return_value=True),
    )
    monkeypatch.setattr(
        endpoint_module,
        "_assert_validator_permitted",
        AsyncMock(return_value=None),
    )
    monkeypatch.setattr(endpoint_module, "consume_validator_nonce", mocks.consume_nonce)
    monkeypatch.setattr(
        endpoint_module,
        "authorize_coding_shadow_harness_delivery",
        mocks.authorize,
    )
    monkeypatch.setattr(endpoint_module, "record_artifact_fetch", mocks.audit)
    return mocks


async def test_harness_launch_is_signed_ticket_bound_and_no_store(
    app: FastAPI,
    client: httpx.AsyncClient,
    session_maker: async_sessionmaker[AsyncSession],
    monkeypatch,
) -> None:
    mocks = _install(app, session_maker, monkeypatch)
    response = await client.post(
        "/api/v1/validator/coding-shadow/harness-launch",
        json=_payload(),
    )
    assert response.status_code == 200, response.text
    assert response.headers["Cache-Control"] == "no-store"
    body = response.json()
    assert body["weight_eligible"] is False
    assert body["ticket_id"] == str(_TICKET_ID)
    assert body["agent_artifact_sha256"] == "55" * 32
    assert body["screened_image_sha256"] == "66" * 32
    assert body["image_url"].startswith("https://storage.invalid/")
    assert mocks.authorize.await_count == 2
    assert all(
        call.kwargs == {"ticket_id": _TICKET_ID, "validator_hotkey": _VALIDATOR}
        for call in mocks.authorize.await_args_list
    )
    mocks.storage.presigned_get_url.assert_awaited_once()
    assert 1 <= mocks.storage.presigned_get_url.await_args.kwargs["expires_in"] <= 300
    mocks.audit.assert_awaited_once()


async def test_harness_launch_rejects_forgery_replay_and_unavailable_authority(
    app: FastAPI,
    client: httpx.AsyncClient,
    session_maker: async_sessionmaker[AsyncSession],
    monkeypatch,
) -> None:
    mocks = _install(app, session_maker, monkeypatch)
    forged = _payload(signature="00" * 64)
    assert (
        await client.post(
            "/api/v1/validator/coding-shadow/harness-launch",
            json=forged,
        )
    ).status_code == 401

    mocks.consume_nonce.side_effect = ValidatorRequestReplayError("replay")
    replay = await client.post(
        "/api/v1/validator/coding-shadow/harness-launch",
        json=_payload(),
    )
    assert replay.status_code == 409

    mocks.consume_nonce.side_effect = None
    mocks.authorize.side_effect = CodingTaskLeaseNotAvailableError("missing")
    unavailable = await client.post(
        "/api/v1/validator/coding-shadow/harness-launch",
        json=_payload(),
    )
    assert unavailable.status_code == 404


async def test_harness_launch_reauthorizes_after_url_mint(
    app: FastAPI,
    client: httpx.AsyncClient,
    session_maker: async_sessionmaker[AsyncSession],
    monkeypatch,
) -> None:
    mocks = _install(app, session_maker, monkeypatch)
    drifted = SimpleNamespace(
        **{
            **vars(mocks.authority),
            "screened_image_sha256": "ff" * 32,
        }
    )
    mocks.authorize.side_effect = [mocks.authority, drifted]
    response = await client.post(
        "/api/v1/validator/coding-shadow/harness-launch",
        json=_payload(),
    )
    assert response.status_code == 409
    assert mocks.storage.presigned_get_url.await_count == 1


async def test_harness_launch_refuses_subsecond_ticket_capability(
    app: FastAPI,
    client: httpx.AsyncClient,
    session_maker: async_sessionmaker[AsyncSession],
    monkeypatch,
) -> None:
    mocks = _install(app, session_maker, monkeypatch)
    mocks.authority.deadline = datetime.now(UTC) + timedelta(milliseconds=500)
    response = await client.post(
        "/api/v1/validator/coding-shadow/harness-launch",
        json=_payload(),
    )
    assert response.status_code == 409
    mocks.storage.presigned_get_url.assert_not_awaited()
    mocks.audit.assert_not_awaited()
