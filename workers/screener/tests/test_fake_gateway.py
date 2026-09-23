"""Tests for the host-side fake OpenAI-compatible screening gateway."""

from __future__ import annotations

import asyncio
import json
import ssl
from pathlib import Path
from urllib.parse import urlsplit

import httpx

from ditto_screener.fake_gateway import FakeModelGateway, tool_capability
from ditto_screener.gate import _write_openrouter_shim_certs
from ditto_screener.runtime_semantics import (
    judge_memory_run,
    judge_ordinary_run,
    judge_tool_run,
)


async def test_ordinary_probe_requires_challenge_forwarded_to_model(
    tmp_path: Path,
) -> None:
    config = tmp_path / "semantic-probe.json"
    events = tmp_path / "semantic-events"
    config.write_text(
        json.dumps(
            {
                "kind": "ordinary",
                "probe_id": "ordinary-1",
                "challenge_token": "bound-question-1",
            }
        )
    )
    async with FakeModelGateway(
        semantic_config_file=str(config), semantic_events_file=str(events)
    ) as gateway:
        url = gateway.gateway_url.replace("host.docker.internal", "127.0.0.1")
        async with httpx.AsyncClient() as client:
            decoy = await client.post(
                f"{url}/v1/chat/completions",
                json={
                    "model": "acme/reasoner",
                    "messages": [{"role": "user", "content": "unrelated question"}],
                    "metadata": {"label": "bound-question-1"},
                },
            )
            assert decoy.status_code == 200
            assert (not events.exists()) or events.read_text() == ""
            assert (
                judge_ordinary_run(
                    {"answer": gateway.response_text},
                    gateway_tokens=(gateway.response_text, "unused"),
                    model_calls=1,
                    events=[],
                ).status
                == "inconclusive"
            )
            forwarded = await client.post(
                f"{url}/v1/chat/completions",
                json={
                    "model": "acme/reasoner",
                    "messages": [
                        {"role": "user", "content": "answer bound-question-1"}
                    ],
                },
            )
    assert forwarded.status_code == 200
    assert [json.loads(line)["event"] for line in events.read_text().splitlines()] == [
        "challenge_seen"
    ]
    assert (
        judge_ordinary_run(
            {"answer": gateway.response_text},
            gateway_tokens=(gateway.response_text, "unused"),
            model_calls=1,
            events=["challenge_seen"],
        ).status
        == "pass"
    )


async def test_semantic_tool_probe_requires_model_emission_and_one_execution(
    tmp_path: Path,
) -> None:
    route = "aBc123_-aBc123_-aBc123_-"
    key = bytes(range(32))
    case_id, user_id, probe_id = "case-private", "user-private", "probe-private"
    config = tmp_path / "semantic-probe.json"
    events = tmp_path / "semantic-events"
    config.write_text(
        json.dumps(
            {
                "kind": "tool",
                "probe_id": probe_id,
                "challenge_token": "challenge-bound-token",
                "case_id": case_id,
                "user_id": user_id,
                "name": "search_web",
                "args": {"query": "private question"},
                "result": "private-result-token",
            }
        )
    )
    common = {
        "semantic_config_file": str(config),
        "semantic_events_file": str(events),
    }
    async with (
        FakeModelGateway(surface="model", **common) as model,
        FakeModelGateway(
            surface="tool", tool_route=route, tool_key=key, **common
        ) as tool,
    ):
        model_url = model.gateway_url.replace("host.docker.internal", "127.0.0.1")
        tool_url = tool.gateway_url.replace("host.docker.internal", "127.0.0.1")
        endpoint = f"{tool_url}/v1/tools/{route}/tool"
        params = {
            "cap": tool_capability(key, case_id, user_id),
            "case_id": case_id,
            "user_id": user_id,
        }
        call = {
            "case_id": case_id,
            "user_id": user_id,
            "name": "search_web",
            "args": {"query": "private question"},
            "hop": 0,
        }
        async with httpx.AsyncClient() as client:
            before_model = await client.post(endpoint, params=params, json=call)
            assert before_model.status_code == 409
            decoy = await client.post(
                f"{model_url}/v1/chat/completions",
                json={
                    "model": "acme/reasoner-v3",
                    "messages": [{"role": "user", "content": "unrelated request"}],
                    "metadata": {"label": "challenge-bound-token"},
                    "tools": [
                        {
                            "type": "function",
                            "function": {"name": "search_web", "parameters": {}},
                        }
                    ],
                },
            )
            assert "tool_calls" not in decoy.json()["choices"][0]["message"]
            assert (not events.exists()) or events.read_text() == ""
            first = await client.post(
                f"{model_url}/v1/chat/completions",
                json={
                    "model": "acme/reasoner-v3",
                    "messages": [
                        {
                            "role": "user",
                            "content": "look this up challenge-bound-token",
                        }
                    ],
                    "tools": [
                        {
                            "type": "function",
                            "function": {"name": "search_web", "parameters": {}},
                        }
                    ],
                },
            )
            tool_call = first.json()["choices"][0]["message"]["tool_calls"][0]
            assert tool_call["function"]["name"] == "search_web"
            assert json.loads(tool_call["function"]["arguments"]) == call["args"]
            wrong_args = await client.post(
                endpoint, params=params, json={**call, "args": {"query": "wrong"}}
            )
            assert wrong_args.status_code == 409
            executed = await client.post(endpoint, params=params, json=call)
            replay = await client.post(endpoint, params=params, json=call)
            assert executed.status_code == 200
            assert executed.json()["result"] == "private-result-token"
            assert replay.status_code == 409
            final = await client.post(
                f"{model_url}/v1/chat/completions",
                json={
                    "model": "acme/reasoner-v3",
                    "messages": [
                        {"role": "tool", "content": executed.json()["result"]}
                    ],
                    "tools": [
                        {
                            "type": "function",
                            "function": {"name": "search_web", "parameters": {}},
                        }
                    ],
                },
            )
        assert final.json()["choices"][0]["message"]["content"] == (
            "private-result-token"
        )
        observed_events = [
            json.loads(line)["event"] for line in events.read_text().splitlines()
        ]
        assert observed_events == ["challenge_seen", "emitted", "executed"]
        assert (
            judge_tool_run(
                {"answer": "private-result-token"},
                expected_result="private-result-token",
                model_calls=model.model_calls,
                events=observed_events,
            ).status
            == "pass"
        )


async def test_semantic_memory_gateway_reveals_only_values_supplied_by_harness(
    tmp_path: Path,
) -> None:
    config = tmp_path / "semantic-probe.json"
    events = tmp_path / "semantic-events"
    config.write_text(
        json.dumps(
            {
                "kind": "memory",
                "markers": ["amber-12", "cobalt-34"],
                "challenges": [
                    {
                        "probe_id": "memory-a",
                        "challenge_token": "question-a",
                        "case_id": "case-a",
                        "user_id": "user-a",
                        "forbidden": "cobalt-34",
                    }
                ],
            }
        )
    )
    route = "aBc123_-aBc123_-aBc123_-"
    key = bytes(range(32))
    async with (
        FakeModelGateway(
            semantic_config_file=str(config), semantic_events_file=str(events)
        ) as gateway,
        FakeModelGateway(
            surface="tool",
            tool_route=route,
            tool_key=key,
            semantic_config_file=str(config),
            semantic_events_file=str(events),
        ) as tool_gateway,
    ):
        url = gateway.gateway_url.replace("host.docker.internal", "127.0.0.1")
        tool_url = tool_gateway.gateway_url.replace("host.docker.internal", "127.0.0.1")
        async with httpx.AsyncClient() as client:
            absent = await client.post(
                f"{url}/v1/chat/completions",
                json={
                    "model": "acme/reasoner",
                    "messages": [{"role": "user", "content": "Recall my value"}],
                },
            )
            present = await client.post(
                f"{url}/v1/chat/completions",
                json={
                    "model": "acme/reasoner",
                    "messages": [{"role": "user", "content": "My value is cobalt-34"}],
                },
            )
            leaked_context = await client.post(
                f"{url}/v1/chat/completions",
                json={
                    "model": "acme/reasoner",
                    "messages": [
                        {
                            "role": "user",
                            "content": "retrieved cobalt-34 from the other user",
                        }
                    ],
                },
            )
            leaked_embedding = await client.post(
                f"{url}/api/embed",
                json={"model": "acme/embed", "input": "cobalt-34"},
            )
            leaked_tool = await client.post(
                f"{tool_url}/v1/tools/{route}/tool",
                params={
                    "cap": tool_capability(key, "case-a", "user-a"),
                    "case_id": "case-a",
                    "user_id": "user-a",
                },
                json={
                    "case_id": "case-a",
                    "user_id": "user-a",
                    "name": "search_web",
                    "args": {"query": "cobalt-34"},
                },
            )
    assert absent.json()["choices"][0]["message"]["content"] == gateway.response_text
    assert present.json()["choices"][0]["message"]["content"] == "cobalt-34"
    assert leaked_context.status_code == 200
    assert leaked_embedding.status_code == 200
    assert leaked_tool.status_code == 200
    observed_events = [
        json.loads(line)["event"] for line in events.read_text().splitlines()
    ]
    assert observed_events == ["cross_user_context"] * 4
    assert (
        judge_memory_run(
            {"answer": "amber-12"},
            expected="amber-12",
            forbidden="cobalt-34",
            model_calls=1,
            events=observed_events,
        ).reason
        == "cross_user_context_disclosed"
    )


async def test_semantic_tool_probe_supports_responses_api(tmp_path: Path) -> None:
    config = tmp_path / "semantic-probe.json"
    events = tmp_path / "semantic-events"
    config.write_text(
        json.dumps(
            {
                "kind": "tool",
                "probe_id": "probe-1",
                "challenge_token": "responses-challenge",
                "case_id": "case-1",
                "user_id": "user-1",
                "name": "search_web",
                "args": {"query": "private question"},
                "result": "private-result-token",
            }
        )
    )
    async with FakeModelGateway(
        semantic_config_file=str(config), semantic_events_file=str(events)
    ) as gateway:
        url = gateway.gateway_url.replace("host.docker.internal", "127.0.0.1")
        async with httpx.AsyncClient() as client:
            first = await client.post(
                f"{url}/v1/responses",
                json={
                    "model": "acme/reasoner",
                    "input": "look this up responses-challenge",
                    "tools": [{"type": "function", "name": "search_web"}],
                },
            )
            second = await client.post(
                f"{url}/v1/responses",
                json={
                    "model": "acme/reasoner",
                    "input": "The tool returned private-result-token",
                    "tools": [{"type": "function", "name": "search_web"}],
                },
            )
    assert first.json()["output"][0]["type"] == "function_call"
    assert first.json()["output"][0]["name"] == "search_web"
    assert second.json()["output_text"] == "private-result-token"


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


async def test_scorer_shaped_tool_route_authenticates_case_and_user() -> None:
    route = "aBc123_-aBc123_-aBc123_-"
    key = bytes(range(32))
    case_id = "c0123456789abcdef"
    user_id = "projected-user"
    async with FakeModelGateway(
        surface="tool", tool_route=route, tool_key=key
    ) as gateway:
        local_url = gateway.gateway_url.replace("host.docker.internal", "127.0.0.1")
        endpoint = f"{local_url}/v1/tools/{route}/tool"
        params = {
            "cap": tool_capability(key, case_id, user_id),
            "case_id": case_id,
            "user_id": user_id,
        }
        async with httpx.AsyncClient() as client:
            preflight = await client.head(endpoint, params=params)
            authorized = await client.post(
                endpoint,
                params=params,
                json={"case_id": case_id, "user_id": user_id, "name": "search_web"},
            )
            wrong_user = await client.post(
                endpoint,
                params=params,
                json={"case_id": case_id, "user_id": "someone-else"},
            )
            wrong_cap = await client.head(endpoint, params={**params, "cap": "wrong"})
            wrong_route = await client.head(
                f"{local_url}/v1/tools/other/tool", params=params
            )
    assert preflight.status_code == 400
    assert authorized.status_code == 200
    assert authorized.json() == {"result": "ok", "error": ""}
    assert wrong_user.status_code == 401
    assert wrong_cap.status_code == 401
    assert wrong_route.status_code == 404
    assert gateway.model_calls == 0
