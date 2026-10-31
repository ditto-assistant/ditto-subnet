"""The Python fallback must not bypass required account billing."""

from contextlib import asynccontextmanager
from dataclasses import replace
from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock
from uuid import uuid4

import httpx
import pytest
from fastapi import FastAPI, HTTPException

from ditto.api_server.endpoints import inference
from ditto.api_server.evaluation_billing import EvaluationBillingConfig


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "stage",
    [
        "competition-chat",
        "competition-embedding",
        "confirmation-reader",
        "confirmation-judge",
        "confirmation-embedding",
    ],
)
async def test_billing_refusal_releases_local_request_without_provider_observation(
    monkeypatch, stage
):
    @asynccontextmanager
    async def transaction():
        yield

    class Session:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *_):
            return None

        def begin(self):
            return transaction()

    request = SimpleNamespace(
        app=SimpleNamespace(
            state=SimpleNamespace(
                session_maker=Session,
                config=SimpleNamespace(
                    evaluation_billing=EvaluationBillingConfig(enabled=True)
                ),
            )
        )
    )
    monkeypatch.setattr(
        inference,
        "begin_paid_evaluation",
        AsyncMock(side_effect=HTTPException(402, "Approve budget")),
    )
    monkeypatch.setattr(
        inference, "confirmation_billing_agent", AsyncMock(return_value=uuid4())
    )
    competition = AsyncMock()
    confirmation = AsyncMock()
    health = AsyncMock(side_effect=AssertionError("billing is not provider health"))
    monkeypatch.setattr(inference, "finish_inference_request", competition)
    monkeypatch.setattr(
        inference, "finish_confirmation_inference_request", confirmation
    )
    monkeypatch.setattr(inference, "record_route_observation", health)
    shared = stage.startswith("confirmation-")
    with pytest.raises(HTTPException) as error:
        await inference._begin_paid_reserved_call(
            request,
            agent_id=uuid4(),
            bundle_id=uuid4() if shared else None,
            grant_id=uuid4(),
            nonce=uuid4(),
            generation=1,
            stage=stage,
            model="frozen/model",
            provider="Frozen",
            body=b"trusted",
        )
    assert error.value.status_code == 402
    finished = confirmation if shared else competition
    finished.assert_awaited_once()
    assert (
        finished.await_args.kwargs["cost_microusd"] == 0
        and finished.await_args.kwargs["status"] == "failed"
    )
    health.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize("claimed_user", [None, "existing-linked-account"])
async def test_python_fallback_refuses_before_account_or_provider_access(
    app: FastAPI,
    client: httpx.AsyncClient,
    monkeypatch: pytest.MonkeyPatch,
    claimed_user: str | None,
) -> None:
    app.state.config = replace(
        app.state.config,
        ditto_router_require_competition_account=True,
        inference_proxy=replace(
            app.state.config.inference_proxy,
            enabled=True,
            openrouter_api_key="test-sponsor-key",
        ),
    )
    # No database/account lookup is needed: this route cannot perform paid
    # inference for ANY identity, including a valid linked account. These
    # seams fail loudly if a rollback reaches account access or admission.
    sessions = Mock(side_effect=AssertionError("must not read a grant"))
    reserve = AsyncMock(side_effect=AssertionError("must not reserve"))
    provider = AsyncMock(side_effect=AssertionError("must not spend sponsor funds"))
    app.state.session_maker = sessions
    monkeypatch.setattr(inference, "begin_inference_request", reserve)
    monkeypatch.setattr(inference, "_complete_chat_with_recovery", provider)
    headers = {
        "X-Ditto-Grant": str(uuid4()),
        "X-Ditto-Generation": "1",
        "X-Ditto-Nonce": str(uuid4()),
        "X-Ditto-Requested-At": datetime.now(UTC).isoformat(),
        "X-Ditto-Proof": "test-proof",
        "Authorization": "Bearer test-bearer",
    }
    if claimed_user is not None:
        headers["X-Ditto-On-Behalf-Of"] = claimed_user
    response = await client.post(
        "/api/v1/inference/chat/completions",
        headers=headers,
        json={
            "model": "openai/gpt-oss-20b",
            "messages": [{"role": "user", "content": "test"}],
            "max_tokens": 64,
        },
    )
    assert response.status_code == 503, response.text
    assert response.json()["message"] == (
        "Ditto-billed competition chat requires the configured relay"
    )
    sessions.assert_not_called()
    reserve.assert_not_awaited()
    provider.assert_not_awaited()


@pytest.mark.asyncio
async def test_python_default_off_keeps_existing_auth_gate(
    app: FastAPI, client: httpx.AsyncClient
) -> None:
    assert app.state.config.ditto_router_require_competition_account is False
    app.state.config = replace(
        app.state.config,
        inference_proxy=replace(
            app.state.config.inference_proxy,
            enabled=True,
            openrouter_api_key="test-sponsor-key",
        ),
    )
    response = await client.post("/api/v1/inference/chat/completions", json={})
    assert response.status_code == 401, response.text
