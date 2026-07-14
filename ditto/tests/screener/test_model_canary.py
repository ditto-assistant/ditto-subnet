"""Tests for the host-side fake OpenAI-compatible screening gateway."""

from __future__ import annotations

import asyncio
import json
from urllib.parse import urlsplit

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


async def test_state_endpoint_reports_observed_model_calls() -> None:
    async with ModelCallCanary() as canary:
        local_url = canary.gateway_url.replace("host.docker.internal", "127.0.0.1")
        async with httpx.AsyncClient() as client:
            before = await client.get(f"{local_url}/__ditto_canary_state")
            await client.post(f"{local_url}/v1/responses", json={"input": "hello"})
            after = await client.get(f"{local_url}/__ditto_canary_state")

        assert before.json() == {"model_calls": 0}
        assert after.json() == {"model_calls": 1}


async def test_chunked_chat_completion_is_accepted() -> None:
    async with ModelCallCanary() as canary:
        port = urlsplit(canary.gateway_url).port
        assert port is not None
        reader, writer = await asyncio.open_connection("127.0.0.1", port)
        writer.write(
            b"POST /v1/chat/completions HTTP/1.1\r\n"
            b"Host: localhost\r\n"
            b"Content-Type: application/json\r\n"
            b"Transfer-Encoding: chunked\r\n\r\n"
            b'9;client=test\r\n{"model":\r\n'
            b'4\r\n"x"}\r\n'
            b"0\r\nX-Request-Trailer: accepted\r\n\r\n"
        )
        await writer.drain()
        raw_response = await reader.read()
        writer.close()
        await writer.wait_closed()

        raw_headers, raw_body = raw_response.split(b"\r\n\r\n", 1)
        assert b" 200 OK\r\n" in raw_headers + b"\r\n"
        assert json.loads(raw_body)["choices"][0]["message"]["content"] == canary.token
        assert canary.model_calls == 1
