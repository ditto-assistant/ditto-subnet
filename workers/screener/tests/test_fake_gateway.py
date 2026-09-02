"""Tests for the host-side fake OpenAI-compatible screening gateway."""

from __future__ import annotations

import asyncio
import json
import ssl
from pathlib import Path
from urllib.parse import urlsplit

import httpx

from ditto_screener.fake_gateway import FakeModelGateway
from ditto_screener.gate import _write_openrouter_shim_certs


async def test_chat_completion_is_counted_and_returns_offline_response(
    tmp_path: Path,
) -> None:
    state = tmp_path / "calls"
    async with FakeModelGateway(state_file=str(state)) as gateway:
        local_url = gateway.gateway_url.replace("host.docker.internal", "127.0.0.1")
        async with httpx.AsyncClient() as client:
            response = await client.post(
                f"{local_url}/v1/chat/completions",
                json={
                    "model": "acme/reasoner-v3",
                    "messages": [{"role": "user", "content": "hello"}],
                },
            )
        assert response.status_code == 200
        body = response.json()
        assert body["choices"][0]["message"]["content"] == gateway.response_text
        # The response must echo the caller's model, use a random id, and a real
        # timestamp so the container cannot fingerprint the screener.
        assert body["model"] == "acme/reasoner-v3"
        assert body["id"].startswith("chatcmpl-")
        assert body["id"] != "chatcmpl-ditto-screening-fake"
        assert body["created"] > 0
        assert gateway.model_calls == 1
        assert state.read_text() == "1\n"
        assert not state.stat().st_mode & 0o077


async def test_existing_rootless_state_file_keeps_cross_uid_write_mode(
    tmp_path: Path,
) -> None:
    state = tmp_path / "calls"
    state.touch(mode=0o600)
    state.chmod(0o622)

    async with FakeModelGateway(state_file=str(state)) as gateway:
        local_url = gateway.gateway_url.replace("host.docker.internal", "127.0.0.1")
        async with httpx.AsyncClient() as client:
            response = await client.post(
                f"{local_url}/v1/chat/completions",
                json={"model": "acme/reasoner-v3", "messages": []},
            )

    assert response.status_code == 200
    assert state.read_text() == "1\n"
    assert state.stat().st_mode & 0o777 == 0o622


async def test_no_screening_fingerprints_leak_in_any_response() -> None:
    """No ``ditto``/``fake``/``screening``/locked-model tell reaches the container."""
    async with FakeModelGateway() as gateway:
        local_url = gateway.gateway_url.replace("host.docker.internal", "127.0.0.1")
        async with httpx.AsyncClient() as client:
            chat = await client.post(
                f"{local_url}/v1/chat/completions",
                json={"model": "acme/x", "messages": []},
            )
            responses = await client.post(
                f"{local_url}/v1/responses", json={"model": "acme/x", "input": "hi"}
            )
            not_found = await client.post(f"{local_url}/v1/unknown", json={})
            bad = await client.post(
                f"{local_url}/v1/chat/completions", content=b"{not-json"
            )
    for raw in (chat.text.lower(), responses.text.lower(), not_found.text.lower()):
        assert "ditto" not in raw
        assert "fake" not in raw
        assert "screening" not in raw
        assert "qwen" not in raw
    # Even the 404/400 error bodies the container can probe must not tell.
    assert "fake" not in bad.text.lower() and "ditto" not in bad.text.lower()


async def test_model_round_trip_oracle_returns_answer_only_after_nonce() -> None:
    """A second turn carrying the nonce yields the answer; a first turn does not."""
    async with FakeModelGateway(
        response_text="nonce-token-abc", oracle_answer="answer-token-xyz"
    ) as gateway:
        local_url = gateway.gateway_url.replace("host.docker.internal", "127.0.0.1")
        async with httpx.AsyncClient() as client:
            first = await client.post(
                f"{local_url}/v1/chat/completions",
                json={"model": "m", "messages": [{"role": "user", "content": "go"}]},
            )
            second = await client.post(
                f"{local_url}/v1/chat/completions",
                json={
                    "model": "m",
                    "messages": [
                        {"role": "assistant", "content": "nonce-token-abc"},
                        {"role": "user", "content": "return that value"},
                    ],
                },
            )
    assert first.json()["choices"][0]["message"]["content"] == "nonce-token-abc"
    assert second.json()["choices"][0]["message"]["content"] == "answer-token-xyz"


async def test_configured_latency_delays_a_model_call() -> None:
    async with FakeModelGateway(latency_range=(0.05, 0.05)) as gateway:
        local_url = gateway.gateway_url.replace("host.docker.internal", "127.0.0.1")
        async with httpx.AsyncClient() as client:
            start = asyncio.get_running_loop().time()
            await client.post(
                f"{local_url}/v1/chat/completions",
                json={"model": "m", "messages": []},
            )
            elapsed = asyncio.get_running_loop().time() - start
    assert elapsed >= 0.05


async def test_concurrent_gateways_keep_call_evidence_isolated(
    tmp_path: Path,
) -> None:
    state_a = tmp_path / "a"
    state_b = tmp_path / "b"
    async with (
        FakeModelGateway(state_file=str(state_a)) as gateway_a,
        FakeModelGateway(state_file=str(state_b)) as gateway_b,
    ):
        urls = [
            gateway_a.gateway_url.replace("host.docker.internal", "127.0.0.1"),
            gateway_b.gateway_url.replace("host.docker.internal", "127.0.0.1"),
        ]
        async with httpx.AsyncClient() as client:
            await asyncio.gather(
                client.post(
                    f"{urls[0]}/v1/chat/completions",
                    json={"model": "x", "messages": []},
                ),
                client.post(
                    f"{urls[1]}/v1/chat/completions",
                    json={"model": "x", "messages": []},
                ),
            )
    assert state_a.read_text() == "1\n"
    assert state_b.read_text() == "1\n"


async def test_embedding_request_does_not_count_as_model_call() -> None:
    async with FakeModelGateway() as gateway:
        local_url = gateway.gateway_url.replace("host.docker.internal", "127.0.0.1")
        async with httpx.AsyncClient() as client:
            response = await client.post(
                f"{local_url}/api/embed", json={"model": "x", "input": "hello"}
            )
        assert response.status_code == 200
        assert response.json()["embeddings"]
        assert gateway.model_calls == 0


async def test_split_gateway_surfaces_do_not_cross_serve_routes() -> None:
    async with (
        FakeModelGateway(surface="model") as model_gateway,
        FakeModelGateway(surface="embedding") as embedding_gateway,
    ):
        model_url = model_gateway.gateway_url.replace(
            "host.docker.internal", "127.0.0.1"
        )
        embedding_url = embedding_gateway.gateway_url.replace(
            "host.docker.internal", "127.0.0.1"
        )
        async with httpx.AsyncClient() as client:
            model_chat = await client.post(
                f"{model_url}/v1/chat/completions",
                json={"model": "x", "messages": []},
            )
            model_embed = await client.post(
                f"{model_url}/api/embed", json={"model": "x", "input": "hello"}
            )
            model_responses = await client.post(
                f"{model_url}/v1/responses", json={"model": "x", "input": "hello"}
            )
            model_health = await client.get(f"{model_url}/health")
            embedding = await client.post(
                f"{embedding_url}/api/embed",
                json={"model": "x", "input": "hello"},
            )
            embedding_chat = await client.post(
                f"{embedding_url}/v1/chat/completions",
                json={"model": "x", "messages": []},
            )

    assert model_chat.status_code == 200
    assert model_health.status_code == 200
    assert embedding.status_code == 200
    assert model_embed.status_code == 404
    assert model_responses.status_code == 404
    assert embedding_chat.status_code == 404


async def test_chunked_chat_completion_is_accepted() -> None:
    async with FakeModelGateway() as gateway:
        port = urlsplit(gateway.gateway_url).port
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
        assert (
            json.loads(raw_body)["choices"][0]["message"]["content"]
            == gateway.response_text
        )
        assert gateway.model_calls == 1


async def test_gateway_answers_with_nonce_content_for_any_caller() -> None:
    """The gateway answers with TEXT content carrying the nonce, whether or not
    the caller declared tools, so a one-turn harness of any architecture relays
    the nonce and passes. A harness that DOES loop and feeds the nonce back gets
    the oracle answer on the second turn, so the multi-turn path still works."""
    async with FakeModelGateway(oracle_answer="oracle-token-xyz") as gateway:
        local_url = gateway.gateway_url.replace("host.docker.internal", "127.0.0.1")
        async with httpx.AsyncClient() as client:
            # Turn 1 (tools declared): a text answer with the nonce, no forced call.
            first = await client.post(
                f"{local_url}/v1/chat/completions",
                json={
                    "model": "acme/reasoner-v3",
                    "messages": [{"role": "user", "content": "look this up"}],
                    "tools": [
                        {
                            "type": "function",
                            "function": {"name": "search_memory", "parameters": {}},
                        }
                    ],
                },
            )
            message = first.json()["choices"][0]["message"]
            assert message["content"] == gateway.response_text
            assert "tool_calls" not in message
            assert first.json()["choices"][0]["finish_reason"] == "stop"

            # A looping harness that feeds the nonce back still unlocks the answer.
            second = await client.post(
                f"{local_url}/v1/chat/completions",
                json={
                    "model": "acme/reasoner-v3",
                    "messages": [
                        {"role": "user", "content": "look this up"},
                        message,
                    ],
                },
            )
            final = second.json()["choices"][0]["message"]
            assert final["content"] == "oracle-token-xyz"
            assert second.json()["choices"][0]["finish_reason"] == "stop"
        assert gateway.model_calls == 2


async def test_text_only_caller_still_gets_a_plain_completion() -> None:
    """No declared tools means no un-executable tool call is forced."""
    async with FakeModelGateway(oracle_answer="oracle-token-xyz") as gateway:
        local_url = gateway.gateway_url.replace("host.docker.internal", "127.0.0.1")
        async with httpx.AsyncClient() as client:
            response = await client.post(
                f"{local_url}/v1/chat/completions",
                json={
                    "model": "acme/reasoner-v3",
                    "messages": [{"role": "user", "content": "hello"}],
                },
            )
        message = response.json()["choices"][0]["message"]
        assert message["content"] == gateway.response_text
        assert "tool_calls" not in message


async def test_openrouter_chat_path_is_a_model_call() -> None:
    """Hardcoded OpenRouter clients POST /api/v1/chat/completions, not /v1."""
    async with FakeModelGateway() as gateway:
        local_url = gateway.gateway_url.replace("host.docker.internal", "127.0.0.1")
        async with httpx.AsyncClient() as client:
            response = await client.post(
                f"{local_url}/api/v1/chat/completions",
                json={"model": "acme/reasoner-v3", "messages": []},
            )
        assert response.status_code == 200
        content = response.json()["choices"][0]["message"]["content"]
        assert content == gateway.response_text
        assert gateway.model_calls == 1


async def test_openrouter_tls_chat_path_is_a_model_call(tmp_path: Path) -> None:
    """The isolated sidecar terminates HTTPS for openrouter.ai:443."""
    _write_openrouter_shim_certs(str(tmp_path))
    assert not (tmp_path / "ca.key").exists()
    assert not (tmp_path / "leaf.csr").exists()
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    context.minimum_version = ssl.TLSVersion.TLSv1_2
    context.load_cert_chain(tmp_path / "leaf.crt", tmp_path / "leaf.key")
    verify = ssl.create_default_context(cafile=str(tmp_path / "ca.crt"))
    async with FakeModelGateway(ssl_context=context) as gateway:
        port = urlsplit(gateway.gateway_url).port
        reader, writer = await asyncio.open_connection(
            "127.0.0.1",
            port,
            ssl=verify,
            server_hostname="openrouter.ai",
        )
        body = json.dumps({"model": "acme/reasoner-v3", "messages": []}).encode()
        writer.write(
            b"POST /api/v1/chat/completions HTTP/1.1\r\n"
            b"Host: openrouter.ai\r\n"
            b"Content-Type: application/json\r\n"
            + f"Content-Length: {len(body)}\r\nConnection: close\r\n\r\n".encode()
            + body
        )
        await writer.drain()
        response = await reader.read()
        writer.close()
        await writer.wait_closed()

        assert gateway.gateway_url.startswith("https://")
        assert response.startswith(b"HTTP/1.1 200")
        assert gateway.model_calls == 1


async def test_tool_sink_returns_result_without_counting_a_model_call(
    tmp_path: Path,
) -> None:
    """The /tool sink lets the oracle's tool-shaped run execute its tool call.

    It returns a benign result so the harness's agent loop proceeds to the
    second model turn, but it MUST NOT increment the gateway model-call count —
    that count is the oracle's round-trip evidence and only model turns count.
    """
    state = tmp_path / "calls"
    async with FakeModelGateway(state_file=str(state)) as gateway:
        local_url = gateway.gateway_url.replace("host.docker.internal", "127.0.0.1")
        async with httpx.AsyncClient() as client:
            response = await client.post(
                f"{local_url}/tool",
                json={
                    "case_id": "c",
                    "name": "search_memories",
                    "args": {"query": "x"},
                },
            )
        assert response.status_code == 200
        body = response.json()
        assert body["result"] and not body["error"]
        assert gateway.model_calls == 0
        assert not state.exists() or state.read_text() == ""
