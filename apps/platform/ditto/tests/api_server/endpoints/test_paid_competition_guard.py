"""The Python fallback must not bypass required account billing."""

from dataclasses import replace
from datetime import UTC, datetime
from unittest.mock import AsyncMock, Mock
from uuid import uuid4

import httpx
import pytest
from fastapi import FastAPI

from ditto.api_server.endpoints import inference


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
