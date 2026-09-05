from __future__ import annotations

from collections.abc import AsyncIterator
from types import SimpleNamespace
from unittest.mock import AsyncMock

import bittensor
import httpx
import pytest
from fastapi import FastAPI
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from ditto.api_models.coding_hosted import hosted_message_digest
from ditto.api_server.coding_hosted_verification import (
    HostedResultExpectation,
    verify_hosted_status,
)
from ditto.api_server.endpoints.validator_coding_hosted import HostedCodingControl
from ditto.db.models import CodingHostedAssignment
from ditto.tests.db.queries.test_coding_hosted_admission import (
    VALIDATOR,
    _request,
    _seed,
)

PLATFORM = bittensor.Keypair.create_from_uri("//Bob")
PATH = "/api/v1/validator/coding-hosted/control"


@pytest.fixture
async def hosted_client(
    app: FastAPI, session_maker: async_sessionmaker[AsyncSession]
) -> AsyncIterator[httpx.AsyncClient]:
    app.state.session_maker = session_maker
    app.state.chain = SimpleNamespace(
        get_recent_neurons=AsyncMock(
            return_value=[
                SimpleNamespace(
                    hotkey=VALIDATOR.ss58_address,
                    validator_permit=True,
                )
            ]
        )
    )
    app.state.coding_hosted_control = HostedCodingControl(PLATFORM)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),
        base_url="http://test",
        headers={"Accept-Encoding": "identity"},
    ) as client:
        yield client


async def test_http_admission_returns_only_signed_status(hosted_client, session_maker):
    authority = await _seed(session_maker)
    request = _request(authority)
    response = await hosted_client.post(
        PATH,
        json={
            **request.model_dump(mode="json", by_alias=True),
            "private_bundle": "PRIVATE_MARKER",
        },
    )
    assert response.status_code == 202, response.text
    assert response.headers["cache-control"] == "no-store"
    assert "PRIVATE_MARKER" not in response.text
    expected = HostedResultExpectation(
        authority.evaluation_id,
        authority.attempt_id,
        authority.validator_hotkey,
        PLATFORM.ss58_address,
        authority.artifact_sha256,
        authority.digest(),
        authority.policy_sha256,
        authority.execution_profile_sha256,
        authority.grading_profile_sha256,
        hosted_message_digest(request),
    )
    status = verify_hosted_status(
        body=response.content,
        expected=expected,
        trusted_verifiers={PLATFORM.ss58_address: PLATFORM},
        now_unix=response.json()["issued_at_unix"],
    )
    assert status.state == "admitted"
    replay = await hosted_client.post(
        PATH, json=request.model_dump(mode="json", by_alias=True)
    )
    assert replay.status_code == 409 and replay.headers["cache-control"] == "no-store"
    async with session_maker() as session:
        row = await session.get(CodingHostedAssignment, authority.evaluation_id)
        assert (
            row is not None and row.admitted_at is not None and row.started_at is None
        )


async def test_disabled_and_malformed_requests_do_not_echo_inputs(app, hosted_client):
    for body, expected in [
        (b"PRIVATE_MARKER" * 1000, 413),
        (b'{"signature":"PRIVATE_MARKER"}', 422),
    ]:
        response = await hosted_client.post(
            PATH, content=body, headers={"Content-Type": "application/json"}
        )
        assert response.status_code == expected
        assert response.headers["cache-control"] == "no-store"
        assert "PRIVATE_MARKER" not in response.text
    app.state.coding_hosted_control = None
    response = await hosted_client.post(PATH, content=b"PRIVATE_MARKER")
    assert response.status_code == 503 and "PRIVATE_MARKER" not in response.text


async def test_streamed_body_limit_ignores_claimed_size(app, hosted_client):
    async def chunks():
        yield b"x" * 4096
        yield b"x" * 4097

    response = await hosted_client.post(
        PATH,
        content=chunks(),
        headers={"Content-Type": "application/json", "Content-Length": "1"},
    )
    assert response.status_code == 413
    assert response.headers["cache-control"] == "no-store"
    app.state.chain.get_recent_neurons.assert_not_awaited()


async def test_invalid_signature_is_denied_before_chain(
    app, hosted_client, session_maker
):
    authority = await _seed(session_maker)
    request = _request(authority).model_copy(update={"signature": "0" * 128})
    response = await hosted_client.post(
        PATH, json=request.model_dump(mode="json", by_alias=True)
    )
    assert response.status_code == 401
    app.state.chain.get_recent_neurons.assert_not_awaited()


async def test_signer_failure_rolls_back_admission_and_nonce(
    app, hosted_client, session_maker
):
    class UnavailableSigner:
        ss58_address = PLATFORM.ss58_address

        def sign(self, data: bytes) -> bytes:
            assert data
            raise RuntimeError("PRIVATE_MARKER")

    authority = await _seed(session_maker)
    request = _request(authority)
    app.state.coding_hosted_control = HostedCodingControl(UnavailableSigner())
    response = await hosted_client.post(
        PATH, json=request.model_dump(mode="json", by_alias=True)
    )
    assert response.status_code == 503 and "PRIVATE_MARKER" not in response.text
    async with session_maker() as session:
        row = await session.get(CodingHostedAssignment, authority.evaluation_id)
        assert row is not None and row.admitted_at is None
    app.state.coding_hosted_control = HostedCodingControl(PLATFORM)
    response = await hosted_client.post(
        PATH, json=request.model_dump(mode="json", by_alias=True)
    )
    assert response.status_code == 202


async def test_unpermitted_validator_does_not_consume_nonce(
    app, hosted_client, session_maker
):
    authority = await _seed(session_maker)
    request = _request(authority)
    app.state.chain.get_recent_neurons.return_value = []
    response = await hosted_client.post(
        PATH, json=request.model_dump(mode="json", by_alias=True)
    )
    assert response.status_code == 401
    async with session_maker() as session:
        row = await session.get(CodingHostedAssignment, authority.evaluation_id)
        assert row is not None and row.admitted_at is None
    app.state.chain.get_recent_neurons.return_value = [
        SimpleNamespace(hotkey=VALIDATOR.ss58_address, validator_permit=True)
    ]
    response = await hosted_client.post(
        PATH, json=request.model_dump(mode="json", by_alias=True)
    )
    assert response.status_code == 202


async def test_status_and_fresh_evaluate_nonces_preserve_attempt(
    hosted_client, session_maker
):
    authority = await _seed(session_maker)
    for operation, state in [
        ("status", "assigned"),
        ("evaluate", "admitted"),
        ("status", "admitted"),
        ("evaluate", "admitted"),
    ]:
        request = _request(authority, operation=operation)
        response = await hosted_client.post(
            PATH, json=request.model_dump(mode="json", by_alias=True)
        )
        assert response.status_code == 202
        assert response.json()["state"] == state
        assert response.json()["attempt_id"] == str(authority.attempt_id)
        assert response.json()["request_sha256"] == hosted_message_digest(request)
        async with session_maker() as session:
            row = await session.get(CodingHostedAssignment, authority.evaluation_id)
            assert row is not None and row.started_at is None
            assert (row.admitted_at is None) == (state == "assigned")
