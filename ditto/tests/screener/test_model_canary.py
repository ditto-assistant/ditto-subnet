"""Tests for the host-side fake OpenAI-compatible screening gateway."""

from __future__ import annotations

import httpx

from ditto.screener.model_canary import ModelCallCanary


async def test_chat_completion_is_counted_and_returns_hidden_token() -> None:
    async with ModelCallCanary() as canary:
        local_url = canary.gateway_url.replace("host.docker.internal", "127.0.0.1")
        async with httpx.AsyncClient() as client:
            response = await client.post(
                f"{local_url}/v1/chat/completions",
                json={
                    "model": "ignored",
                    "messages": [{"role": "user", "content": "hello"}],
                },
            )
        assert response.status_code == 200
        assert response.json()["choices"][0]["message"]["content"] == canary.token
        assert canary.model_calls == 1


async def test_embedding_request_does_not_count_as_model_call() -> None:
    async with ModelCallCanary() as canary:
        local_url = canary.gateway_url.replace("host.docker.internal", "127.0.0.1")
        async with httpx.AsyncClient() as client:
            response = await client.post(
                f"{local_url}/api/embed", json={"model": "x", "input": "hello"}
            )
        assert response.status_code == 200
        assert response.json()["embeddings"]
        assert canary.model_calls == 0
