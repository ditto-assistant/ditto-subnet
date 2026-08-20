import base64
import copy
import json
from datetime import UTC, datetime
from types import SimpleNamespace
from typing import Any, cast
from uuid import UUID, uuid4

import bittensor
import httpx
import pytest
from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from fastapi import HTTPException

from ditto.api_models.inference import (
    InferenceExchangeRequest,
    InferenceExchangeResponse,
    InferenceGrantOffer,
)
from ditto.api_server.config import InferenceProxyConfig
from ditto.api_server.endpoints.inference import (
    _ALLOWED_REQUEST_FIELDS,
    _DROPPED_REQUEST_FIELDS,
    _FORWARDED_REQUEST_FIELDS,
    _PINNED_REQUEST_FIELDS,
    _REFUSED_REQUEST_FIELDS,
    _bounded_provider_cost,
    _complete_chat_with_recovery,
    _estimated_tokens,
    _exchange_message,
    _locked_confirmation_chat_payload,
    _locked_grant_model,
    _locked_upstream_payload,
    _max_chargeable_tokens,
    _openrouter_attempt_count,
    _openrouter_headers,
    _openrouter_last_attempted_provider,
    _output_token_limit,
    _perplexity_embedding_response,
    _post_embedding_provider,
    _post_provider_with_retry,
    _provider_is_backpressure,
    _provider_preferences,
    _provider_rejection_is_route_observable,
    _provider_retry_after_seconds,
    _ProviderCallError,
    _proxy_message,
    _public_embedding_response,
    _public_provider_response,
    _reliability_provider_preferences,
    _upstream_provider,
    _uses_hosted_embeddings,
    _validate_request_schema,
    _validated_embedding_payload,
)
from ditto.api_server.endpoints.validator import _verify_signature


def test_hosted_embedding_contract_is_inherited_by_later_benches() -> None:
    assert not _uses_hosted_embeddings(6)
    assert _uses_hosted_embeddings(7)
    assert _uses_hosted_embeddings(8)
    assert _uses_hosted_embeddings(9)
    assert _uses_hosted_embeddings(10)


def _embedding_config(**overrides: Any) -> SimpleNamespace:
    values = {
        "embedding_upstream_url": "https://openrouter.ai/api/v1/embeddings",
        "embedding_fallback_url": "https://api.perplexity.ai/v1/embeddings",
        "embedding_model": "perplexity/pplx-embed-v1-0.6b",
        "embedding_provider": "Perplexity",
        "embedding_dimensions": 768,
        "openrouter_api_key": "openrouter-test-key",
        "perplexity_api_key": "perplexity-test-key",
    }
    values.update(overrides)
    return SimpleNamespace(**values)


@pytest.mark.asyncio
async def test_embedding_gateway_falls_back_to_openrouter_on_direct_429() -> None:
    requests: list[httpx.Request] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        if request.url.host == "api.perplexity.ai":
            return httpx.Response(429, request=request, headers={"Retry-After": "5"})
        payload = json.loads(request.content)
        assert payload == {
            "model": "perplexity/pplx-embed-v1-0.6b",
            "input": ["private input"],
            "dimensions": 768,
            "encoding_format": "float",
            "provider": {
                "order": ["Perplexity"],
                "allow_fallbacks": False,
                "data_collection": "deny",
            },
        }
        assert request.headers["Authorization"] == "Bearer openrouter-test-key"
        return httpx.Response(200, request=request, json={"data": []})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        result = await _post_embedding_provider(
            client, config=_embedding_config(), inputs=["private input"]
        )
    assert not result.direct
    assert result.attempts == 2
    assert [request.url.host for request in requests] == [
        "api.perplexity.ai",
        "openrouter.ai",
    ]


@pytest.mark.asyncio
async def test_embedding_gateway_does_not_fallback_on_direct_request_error() -> None:
    requests: list[httpx.Request] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(400, request=request)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        result = await _post_embedding_provider(
            client, config=_embedding_config(), inputs=["private input"]
        )
    assert result.direct
    assert result.response.status_code == 400
    assert len(requests) == 1


@pytest.mark.asyncio
async def test_embedding_gateway_keeps_direct_perplexity_primary_when_healthy() -> None:
    requests: list[httpx.Request] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(200, request=request, json={"data": []})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        result = await _post_embedding_provider(
            client, config=_embedding_config(), inputs=["private input"]
        )
    assert result.direct
    assert result.response.status_code == 200
    assert len(requests) == 1
    assert requests[0].url.host == "api.perplexity.ai"


@pytest.mark.asyncio
async def test_direct_embedding_primary_avoids_hung_openrouter() -> None:
    requests: list[httpx.Request] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        if request.url.host == "openrouter.ai":
            raise httpx.ReadTimeout("router must not be reached", request=request)
        return httpx.Response(200, request=request, json={"data": []})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        result = await _post_embedding_provider(
            client, config=_embedding_config(), inputs=["private input"]
        )
    assert result.direct
    assert result.attempts == 1
    assert [request.url.host for request in requests] == ["api.perplexity.ai"]


def test_direct_perplexity_int8_conversion_matches_openrouter_float_contract() -> None:
    signed = [-128, -1, 0, 1, 127]
    encoded = base64.b64encode(bytes(value & 0xFF for value in signed)).decode()
    converted = _perplexity_embedding_response(
        {
            "model": "pplx-embed-v1-0.6b",
            "data": [{"object": "embedding", "index": 0, "embedding": encoded}],
            "usage": {"prompt_tokens": 5, "total_tokens": 5},
        }
    )
    assert converted["data"][0]["embedding"] == [value / 128 for value in signed]


@pytest.mark.parametrize("encoded", ["not base64!", base64.b64encode(b"").decode()])
def test_direct_perplexity_conversion_fails_closed_on_invalid_vectors(
    encoded: str,
) -> None:
    try:
        converted = _perplexity_embedding_response(
            {
                "model": "pplx-embed-v1-0.6b",
                "data": [{"index": 0, "embedding": encoded}],
                "usage": {"prompt_tokens": 1},
            }
        )
    except HTTPException as error:
        assert error.status_code == 502
        return
    with pytest.raises(HTTPException, match="invalid provider response"):
        _public_embedding_response(
            converted,
            model="perplexity/pplx-embed-v1-0.6b",
            dimensions=768,
            input_count=1,
        )


@pytest.mark.asyncio
async def test_provider_retry_policy_retries_explicit_transient_statuses() -> None:
    statuses = iter((503, 429, 200))
    calls = 0

    async def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return httpx.Response(next(statuses), request=request)

    async def no_sleep(_: float) -> None:
        return None

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        result = await _post_provider_with_retry(
            client,
            "https://provider.example/v1/request",
            payload={"model": "test"},
            headers={},
            sleep=no_sleep,
        )

    assert result.response.status_code == 200
    assert result.attempts == 3
    assert calls == 3


@pytest.mark.asyncio
async def test_provider_retry_policy_honors_bounded_backpressure_hints() -> None:
    statuses = iter((429, 503, 200))
    sleeps: list[float] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            next(statuses), headers={"Retry-After": "120"}, request=request
        )

    async def record_sleep(delay: float) -> None:
        sleeps.append(delay)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        result = await _post_provider_with_retry(
            client,
            "https://provider.example/v1/request",
            payload={"model": "test"},
            headers={},
            sleep=record_sleep,
        )

    assert result.response.status_code == 200
    assert result.attempts == 3
    assert sleeps == [5, 5]


@pytest.mark.asyncio
async def test_provider_retry_policy_can_delegate_backpressure_to_caller() -> None:
    calls = 0

    async def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return httpx.Response(429, headers={"Retry-After": "2"}, request=request)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        result = await _post_provider_with_retry(
            client,
            "https://provider.example/v1/request",
            payload={"model": "test"},
            headers={},
            retry_backpressure=False,
        )

    assert result.response.status_code == 429
    assert result.attempts == 1
    assert calls == 1


@pytest.mark.parametrize(
    ("header", "expected"),
    [("", 1), ("invalid", 1), ("0", 1), ("3", 3), ("999", 5)],
)
def test_provider_retry_after_is_bounded(header: str, expected: int) -> None:
    response = httpx.Response(429, headers={"Retry-After": header})
    assert _provider_retry_after_seconds(response) == expected


@pytest.mark.parametrize(
    ("status", "header", "expected"),
    [(429, "", True), (503, "2", True), (503, "", False), (502, "2", False)],
)
def test_provider_backpressure_classification_is_narrow(
    status: int, header: str, expected: bool
) -> None:
    response = httpx.Response(status, headers={"Retry-After": header})
    assert _provider_is_backpressure(response) is expected


@pytest.mark.asyncio
async def test_provider_retry_policy_does_not_repeat_ambiguous_read_timeout() -> None:
    calls = 0

    async def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        raise httpx.ReadTimeout("provider response timed out", request=request)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        with pytest.raises(_ProviderCallError) as raised:
            await _post_provider_with_retry(
                client,
                "https://provider.example/v1/request",
                payload={"model": "test"},
                headers={},
            )

    assert raised.value.attempts == 1
    assert raised.value.timed_out is True
    assert calls == 1


def _exchange(keypair: bittensor.Keypair) -> InferenceExchangeRequest:
    private = Ed25519PrivateKey.generate()
    public = private.public_key().public_bytes_raw()
    unsigned = InferenceExchangeRequest(
        validator_hotkey=keypair.ss58_address,
        grant_id=uuid4(),
        broker_public_key=base64.urlsafe_b64encode(public).decode().rstrip("="),
        nonce=uuid4(),
        requested_at=datetime.now(UTC),
        signature="00" * 64,
    )
    return unsigned.model_copy(
        update={"signature": keypair.sign(_exchange_message(unsigned)).hex()}
    )


def test_forged_validator_and_valid_validator_wrong_ticket_fail() -> None:
    validator = bittensor.Keypair.create_from_uri("//Alice")
    forger = bittensor.Keypair.create_from_uri("//Bob")
    request = _exchange(validator)
    assert _verify_signature(
        validator.ss58_address, _exchange_message(request), request.signature
    )
    assert not _verify_signature(
        validator.ss58_address,
        _exchange_message(request),
        forger.sign(_exchange_message(request)).hex(),
    )
    wrong_ticket = request.model_copy(update={"grant_id": uuid4()})
    assert not _verify_signature(
        validator.ss58_address,
        _exchange_message(wrong_ticket),
        request.signature,
    )


def test_broker_proof_binds_generation_nonce_time_and_exact_body() -> None:
    private = Ed25519PrivateKey.generate()
    public = private.public_key()
    grant_id = uuid4()
    nonce = uuid4()
    requested_at = datetime.now(UTC)
    body = b'{"model":"qwen/qwen3-32b","messages":[]}'

    def message(generation: int, request_nonce: UUID, request_body: bytes) -> bytes:
        return _proxy_message(
            grant_id=grant_id,
            generation=generation,
            nonce=request_nonce,
            requested_at=requested_at,
            body=request_body,
        )

    signature = private.sign(message(2, nonce, body))
    public.verify(signature, message(2, nonce, body))
    for changed in (
        message(3, nonce, body),
        message(2, uuid4(), body),
        message(2, nonce, body + b" "),
    ):
        with pytest.raises(InvalidSignature):
            public.verify(signature, changed)


def test_embedding_contract_is_exact_and_response_is_sanitized() -> None:
    model = "perplexity/pplx-embed-v1-0.6b"
    payload = {
        "model": model,
        "input": ["one", "two"],
        "dimensions": 768,
        "encoding_format": "float",
    }
    assert _validated_embedding_payload(payload, model=model, dimensions=768) == [
        "one",
        "two",
    ]
    for changed in (
        {**payload, "model": "attacker/model"},
        {**payload, "dimensions": 1536},
        {**payload, "provider": {"allow_fallbacks": True}},
        {**payload, "input": []},
    ):
        with pytest.raises(HTTPException):
            _validated_embedding_payload(changed, model=model, dimensions=768)

    vector = [0.0] * 768
    public, prompt_tokens = _public_embedding_response(
        {
            "object": "list",
            "model": "pplx-embed-v1-0.6b",
            "provider": "must-not-leak",
            "data": [
                {"object": "embedding", "index": 0, "embedding": vector},
                {"object": "embedding", "index": 1, "embedding": vector},
            ],
            "usage": {"prompt_tokens": 7, "total_tokens": 7, "cost": 1},
        },
        model=model,
        dimensions=768,
        input_count=2,
    )
    assert prompt_tokens == 7
    assert public["model"] == model
    assert public["usage"] == {"prompt_tokens": 7, "total_tokens": 7}
    assert "provider" not in public
    assert "cost" not in str(public)

    for response_model in (
        "Perplexity/pplx-embed-v1-0.6b",
        "pplx-embed-v1-0.6b:latest",
        "attacker/model",
    ):
        with pytest.raises(HTTPException, match="provider identity mismatch"):
            _public_embedding_response(
                {
                    "object": "list",
                    "model": response_model,
                    "data": [
                        {"object": "embedding", "index": 0, "embedding": vector},
                        {"object": "embedding", "index": 1, "embedding": vector},
                    ],
                    "usage": {"prompt_tokens": 7, "total_tokens": 7},
                },
                model=model,
                dimensions=768,
                input_count=2,
            )


def test_output_token_alias_cannot_bypass_ticket_limit() -> None:
    # Disagreeing aliases resolve downward, so neither can raise the other.
    assert (
        _output_token_limit({"max_tokens": 1, "max_completion_tokens": 999_999}, 8192)
        == 1
    )
    assert (
        _output_token_limit({"max_tokens": 999_999, "max_completion_tokens": 1}, 8192)
        == 1
    )
    # An over-ask is clamped to the ticket ceiling instead of killing the run.
    assert _output_token_limit({"max_completion_tokens": 8193}, 8192) == 8192
    assert _output_token_limit({"max_tokens": 10**9}, 8192) == 8192
    assert _output_token_limit({"max_completion_tokens": 32}, 8192) == 32
    assert _output_token_limit({}, 8192) == 8192
    # A non-positive or non-integer ceiling has no conservative normalisation
    # and stays a named refusal.
    for key, bad in (
        ("max_tokens", {"max_tokens": 0}),
        ("max_tokens", {"max_tokens": -1}),
        ("max_completion_tokens", {"max_completion_tokens": True}),
        ("max_completion_tokens", {"max_completion_tokens": "many"}),
    ):
        with pytest.raises(HTTPException) as invalid:
            _output_token_limit(bad, 8192)
        assert str(invalid.value.detail) == f"invalid {key}"


@pytest.mark.parametrize(
    "escape",
    [
        {"models": ["attacker/model"]},
        {"plugins": [{"id": "web"}]},
        {"transforms": ["middle-out"]},
        {
            "messages": [
                {
                    "role": "user",
                    "content": [{"type": "image_url", "image_url": "http://local"}],
                }
            ]
        },
        {"tools": [{"type": "web_search", "web_search": {}}]},
    ],
)
def test_proxy_schema_rejects_model_and_network_escapes(
    escape: dict[str, object],
) -> None:
    payload: dict[str, object] = {
        "model": "openai/gpt-oss-20b",
        "messages": [{"role": "user", "content": "hello"}],
    }
    payload.update(escape)
    with pytest.raises(HTTPException):
        _validate_request_schema(payload)


def test_proxy_schema_drops_openrouter_routing_hints() -> None:
    """lets_5.6 injects OpenRouter ``provider``; we pin routing ourselves."""
    payload: dict[str, object] = {
        "model": "openai/gpt-oss-20b",
        "messages": [{"role": "user", "content": "hello"}],
        "provider": {
            "only": ["parasail", "amazon-bedrock", "novita"],
            "allow_fallbacks": True,
        },
        "route": "fallback",
        "preset": "fast",
    }
    _validate_request_schema(payload)
    upstream = _locked_upstream_payload(
        payload, model="openai/gpt-oss-20b", max_tokens=256
    )
    assert "provider" not in upstream
    assert "route" not in upstream
    assert "preset" not in upstream


def test_proxy_schema_allows_only_local_function_tools() -> None:
    _validate_request_schema(
        {
            "model": "openai/gpt-oss-20b",
            "messages": [{"role": "user", "content": "hello"}],
            "tools": [
                {
                    "type": "function",
                    "function": {
                        "name": "lookup",
                        "description": "local harness tool",
                        "parameters": {
                            "type": "object",
                            "properties": {"query": {"type": "string"}},
                        },
                    },
                }
            ],
        }
    )


def test_proxy_schema_accepts_exact_openai_text_content_parts() -> None:
    _validate_request_schema(
        {
            "model": "openai/gpt-oss-20b",
            "messages": [
                {
                    "role": "system",
                    "content": [{"type": "text", "text": "follow the tools"}],
                },
                {"role": "user", "content": "hello"},
            ],
        }
    )


@pytest.mark.parametrize(
    "content",
    [
        [],
        [{"type": "image_url", "image_url": {"url": "https://example.test"}}],
        [{"type": "text", "text": "hello", "url": "https://example.test"}],
        [{"type": "text", "text": 1}],
    ],
)
def test_proxy_schema_rejects_non_text_content_parts(content: object) -> None:
    with pytest.raises(HTTPException, match="text content only"):
        _validate_request_schema(
            {
                "model": "openai/gpt-oss-20b",
                "messages": [{"role": "user", "content": content}],
            }
        )


@pytest.mark.parametrize(
    "parameter",
    [
        {"temperature": float("nan")},
        {"temperature": 2.01},
        {"top_p": -0.01},
        {"top_p": "fast"},
        {"seed": True},
        {"seed": 2**63},
        {"stop": ["a", "b", "c", "d", "e"]},
        {"stop": ["ok", 1]},
        {"parallel_tool_calls": 1},
        {"stream": "false"},
        {"n": True},
        {"n": 0},
        {"best_of": 0},
        {"frequency_penalty": 2.5},
        {"presence_penalty": float("inf")},
        {"store": "yes"},
        {"tool_choice": {"type": "function", "function": {"name": ""}}},
        {"tool_choice": {"type": "function", "function": {"name": "x", "x": 1}}},
    ],
)
def test_proxy_schema_rejects_invalid_or_amplifying_scalar_controls(
    parameter: dict[str, object],
) -> None:
    payload: dict[str, object] = {
        "model": "openai/gpt-oss-20b",
        "messages": [{"role": "user", "content": "hello"}],
    }
    payload.update(parameter)
    with pytest.raises(HTTPException):
        _validate_request_schema(payload)


def test_proxy_schema_accepts_bounded_scalar_controls() -> None:
    _validate_request_schema(
        {
            "model": "openai/gpt-oss-20b",
            "messages": [{"role": "user", "content": "hello"}],
            "temperature": 0.7,
            "top_p": 0.9,
            "seed": -(2**63),
            "stop": ["done"],
            "parallel_tool_calls": False,
            "stream": False,
            "n": 1,
            "tool_choice": {
                "type": "function",
                "function": {"name": "lookup"},
            },
        }
    )


def test_tool_result_name_is_accepted_then_removed_from_provider_payload() -> None:
    """Legacy-compatible tool annotations must not make a run unevaluable."""
    payload: dict[str, Any] = {
        "model": "openai/gpt-oss-20b",
        "messages": [
            {"role": "user", "content": "look it up"},
            {
                "role": "assistant",
                "content": "",
                "tool_calls": [
                    {
                        "id": "call-1",
                        "type": "function",
                        "function": {"name": "lookup", "arguments": "{}"},
                    }
                ],
            },
            {
                "role": "tool",
                "tool_call_id": "call-1",
                "name": "lookup",
                "content": '{"result":"ok"}',
            },
        ],
    }

    _validate_request_schema(payload)
    upstream = _locked_upstream_payload(
        payload, model="openai/gpt-oss-20b", max_tokens=256
    )

    assert upstream["messages"][2] == {
        "role": "tool",
        "tool_call_id": "call-1",
        "content": '{"result":"ok"}',
    }
    assert payload["messages"][2]["name"] == "lookup"


@pytest.mark.parametrize("name", ["", None, 7])
def test_tool_result_name_must_be_a_non_empty_string(name: object) -> None:
    with pytest.raises(HTTPException) as raised:
        _validate_request_schema(
            {
                "model": "openai/gpt-oss-20b",
                "messages": [
                    {
                        "role": "tool",
                        "tool_call_id": "call-1",
                        "name": name,
                        "content": "ok",
                    }
                ],
            }
        )

    assert raised.value.status_code == 400
    assert raised.value.detail == "invalid tool name"


def test_aggregate_route_is_throughput_sorted_and_excludes_unreviewed_routes() -> None:
    assert _provider_preferences(
        routing_mode="aggregate_throughput",
        provider="openrouter",
        quantization=None,
    ) == {
        "sort": "throughput",
        "ignore": ["coreweave"],
        "allow_fallbacks": True,
        "data_collection": "deny",
        "zdr": True,
    }
    assert _reliability_provider_preferences() == {
        "order": ["deepinfra", "groq"],
        "ignore": ["coreweave"],
        "allow_fallbacks": False,
        "data_collection": "deny",
        "zdr": True,
    }
    assert _bounded_provider_cost({"usage": {"cost": 0.012345}}) == 12_345
    assert _bounded_provider_cost({"usage": {"cost": float("nan")}}) is None


def _recovery_config() -> InferenceProxyConfig:
    return cast(
        InferenceProxyConfig,
        SimpleNamespace(
            routing_mode="aggregate_throughput",
            upstream_url="https://openrouter.ai/api/v1/chat/completions",
            openrouter_api_key="test-key",
            response_body_bytes=2 << 20,
        ),
    )


def _provider_completion(*, provider: str | None, attempt: int = 1) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "id": "gen-test",
        "object": "chat.completion",
        "created": 1,
        "model": "openai/gpt-oss-20b",
        "choices": [
            {
                "index": 0,
                "finish_reason": "stop",
                "message": {"role": "assistant", "content": "OK"},
            }
        ],
        "usage": {
            "prompt_tokens": 10,
            "completion_tokens": 2,
            "cost": 0.00000135,
        },
    }
    if provider is not None:
        payload["openrouter_metadata"] = {
            "attempt": attempt,
            "endpoints": {"available": [{"provider": provider, "selected": True}]},
        }
    return payload


@pytest.mark.asyncio
async def test_invalid_fast_response_recovers_through_deepinfra() -> None:
    requests: list[dict[str, Any]] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        requests.append(body)
        if len(requests) == 1:
            return httpx.Response(
                200,
                request=request,
                json={"model": "openai/gpt-oss-20b", "choices": []},
            )
        return httpx.Response(
            200,
            request=request,
            json=_provider_completion(provider="DeepInfra", attempt=2),
        )

    async def no_sleep(_: float) -> None:
        return None

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        result = await _complete_chat_with_recovery(
            client,
            _recovery_config(),
            payload={"model": "openai/gpt-oss-20b", "messages": []},
            model="openai/gpt-oss-20b",
            expected_provider="openrouter",
            expected_quantization=None,
            expected_prompt_price=None,
            expected_completion_price=None,
            sleep=no_sleep,
        )

    assert requests[0]["provider"]["sort"] == "throughput"
    assert requests[1]["provider"]["order"] == ["deepinfra", "groq"]
    assert result.upstream_provider == "DeepInfra"
    assert result.fallback_phase == 1
    assert result.upstream_attempts == 2
    assert result.openrouter_attempts == 2


def test_openrouter_headers_attribute_chat_and_embedding_traffic() -> None:
    assert _openrouter_headers("secret") == {
        "Authorization": "Bearer secret",
        "Content-Type": "application/json",
        "HTTP-Referer": "https://heyditto.ai/",
        "X-OpenRouter-Title": "Ditto",
    }


def test_openrouter_failure_metadata_preserves_internal_attempts() -> None:
    payload = {
        "openrouter_metadata": {
            "attempt": 2,
            "attempts": [
                {"provider": "DeepInfra", "status": 502},
                {"provider": "Groq", "status": 503},
            ],
            "endpoints": {
                "available": [
                    {"provider": "DeepInfra", "selected": False},
                    {"provider": "Groq", "selected": False},
                ]
            },
        }
    }
    assert _openrouter_attempt_count(payload) == 2
    assert _openrouter_last_attempted_provider(payload) == "Groq"
    assert _openrouter_headers("secret", include_metadata=True) == {
        "Authorization": "Bearer secret",
        "Content-Type": "application/json",
        "HTTP-Referer": "https://heyditto.ai/",
        "X-OpenRouter-Title": "Ditto",
        "X-OpenRouter-Metadata": "enabled",
    }


def test_v7_upstream_profile_pins_medium_reasoning_without_changing_v6() -> None:
    payload = {
        "model": "attacker/model",
        "messages": [{"role": "user", "content": "hello"}],
        "max_completion_tokens": 999,
        "stream": False,
    }
    v7 = _locked_upstream_payload(payload, model="openai/gpt-oss-20b", max_tokens=256)
    assert v7["model"] == "openai/gpt-oss-20b"
    assert v7["max_tokens"] == 256
    assert v7["n"] == 1
    assert v7["stream"] is False
    assert v7["reasoning"] == {"effort": "medium", "exclude": True}
    assert "max_completion_tokens" not in v7

    v6 = _locked_upstream_payload(payload, model="qwen/qwen3-32b", max_tokens=256)
    assert "reasoning" not in v6


def _confirmation_grant(*, lane: str, model: str) -> SimpleNamespace:
    return SimpleNamespace(lane=lane, model=model, route_provider="deepinfra")


def _confirmation_reader_payload(model: str, **extra: Any) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "model": model,
        "messages": [{"role": "user", "content": "memory"}],
        "max_tokens": 64,
        "provider": {
            "sort": "throughput",
            "ignore": ["coreweave"],
            "allow_fallbacks": True,
            "data_collection": "deny",
        },
    }
    payload.update(extra)
    return payload


def test_confirmation_reader_applies_v9_gpt_oss_reasoning_contract() -> None:
    grant = _confirmation_grant(lane="reader", model="openai/gpt-oss-20b")
    upstream, max_tokens = _locked_confirmation_chat_payload(
        _confirmation_reader_payload(
            "openai/gpt-oss-20b", user="miner", metadata={"k": "v"}
        ),
        grant=grant,
        max_output_tokens=128,
    )
    assert max_tokens == 64
    assert upstream["reasoning"] == {"effort": "medium", "exclude": True}
    assert "reasoning_effort" not in upstream
    assert "user" not in upstream
    assert "metadata" not in upstream
    assert upstream["provider"]["zdr"] is True
    assert upstream["provider"]["sort"] == "throughput"
    assert upstream["provider"]["ignore"] == ["coreweave"]
    assert "only" not in upstream["provider"]
    assert upstream["usage"] == {"include": True}


def test_confirmation_reader_rejects_vendor_pin() -> None:
    grant = _confirmation_grant(lane="reader", model="openai/gpt-oss-20b")
    payload = _confirmation_reader_payload("openai/gpt-oss-20b")
    payload["provider"] = {
        "only": ["deepinfra"],
        "order": ["deepinfra"],
        "allow_fallbacks": False,
        "require_parameters": True,
        "data_collection": "deny",
    }
    with pytest.raises(HTTPException) as raised:
        _locked_confirmation_chat_payload(payload, grant=grant, max_output_tokens=128)
    assert raised.value.status_code == 403
    assert raised.value.detail == "confirmation route is not permitted"


def test_confirmation_reader_canonicalizes_reasoning_effort_alias() -> None:
    grant = _confirmation_grant(lane="reader", model="openai/gpt-oss-20b")
    upstream, _ = _locked_confirmation_chat_payload(
        _confirmation_reader_payload("openai/gpt-oss-20b", reasoning_effort="high"),
        grant=grant,
        max_output_tokens=128,
    )
    assert "reasoning_effort" not in upstream
    assert upstream["reasoning"] == {"effort": "high", "exclude": True}


def test_confirmation_reader_heals_conflicting_reasoning_aliases() -> None:
    grant = _confirmation_grant(lane="reader", model="openai/gpt-oss-20b")
    upstream, _ = _locked_confirmation_chat_payload(
        _confirmation_reader_payload(
            "openai/gpt-oss-20b",
            reasoning={"effort": "low"},
            reasoning_effort="high",
        ),
        grant=grant,
        max_output_tokens=128,
    )
    assert "reasoning_effort" not in upstream
    assert upstream["reasoning"] == {"effort": "low", "exclude": True}


def test_confirmation_judge_does_not_gain_gpt_oss_reasoning() -> None:
    grant = _confirmation_grant(lane="judge", model="openai/gpt-4o-2024-08-06")
    payload = {
        "model": "openai/gpt-4o-2024-08-06",
        "messages": [{"role": "user", "content": "memory"}],
        "max_tokens": 64,
        "provider": {
            "only": ["deepinfra"],
            "order": ["deepinfra"],
            "allow_fallbacks": False,
            "require_parameters": True,
            "data_collection": "deny",
        },
    }
    upstream, max_tokens = _locked_confirmation_chat_payload(
        payload, grant=grant, max_output_tokens=128
    )
    assert max_tokens == 64
    assert "reasoning" not in upstream
    assert "max_tokens" not in upstream
    assert upstream["max_completion_tokens"] == 64


def test_caller_reasoning_is_accepted_then_overwritten_with_the_pinned_effort() -> None:
    """The rejection that cost `Cooking` three submissions is now a normalisation.

    The platform already stamps `{"effort": "medium", "exclude": True}` on every
    v7 request (`benchmark_reasoning`), so the miner was 400'd for redundantly
    requesting exactly what it was being given. Accepting the field is safe for
    the same reason it was pointless to refuse it: the value is replaced.
    """
    for effort in ({"effort": "high"}, {"effort": "low"}, {"max_tokens": 100_000}, {}):
        payload = {
            "model": "openai/gpt-oss-20b",
            "messages": [{"role": "user", "content": "hello"}],
            "reasoning": effort,
        }
        _validate_request_schema(payload)
        upstream = _locked_upstream_payload(
            payload, model="openai/gpt-oss-20b", max_tokens=256
        )
        assert upstream["reasoning"] == {"effort": "medium", "exclude": True}

    # `reasoning_effort` is the *flat* OpenAI sibling and a different key. It is
    # accepted so no harness dies on it, and dropped so it cannot compete with
    # the pinned nested value at the provider.
    flat = {
        "model": "openai/gpt-oss-20b",
        "messages": [{"role": "user", "content": "hello"}],
        "reasoning_effort": "high",
    }
    _validate_request_schema(flat)
    upstream = _locked_upstream_payload(
        flat, model="openai/gpt-oss-20b", max_tokens=256
    )
    assert "reasoning_effort" not in upstream
    assert upstream["reasoning"] == {"effort": "medium", "exclude": True}


def test_v9_omitted_reasoning_defaults_to_medium_without_mutating_input() -> None:
    payload = {
        "model": "openai/gpt-oss-20b",
        "messages": [{"role": "user", "content": "hello"}],
    }
    before = copy.deepcopy(payload)

    upstream = _locked_upstream_payload(
        payload,
        model="openai/gpt-oss-20b",
        max_tokens=256,
        bench_version=9,
    )

    assert upstream["reasoning"] == {"effort": "medium", "exclude": True}
    assert payload == before


@pytest.mark.parametrize("effort", ["low", "medium", "high"])
def test_v9_flat_reasoning_effort_is_canonicalized(effort: str) -> None:
    payload = {
        "model": "openai/gpt-oss-20b",
        "messages": [{"role": "user", "content": "hello"}],
        "reasoning_effort": effort,
    }

    upstream = _locked_upstream_payload(
        payload,
        model="openai/gpt-oss-20b",
        max_tokens=256,
        bench_version=9,
    )

    assert upstream["reasoning"] == {"effort": effort, "exclude": True}
    assert "reasoning_effort" not in upstream
    assert payload["reasoning_effort"] == effort


@pytest.mark.parametrize("effort", ["low", "medium", "high"])
def test_v9_nested_reasoning_effort_is_canonicalized(effort: str) -> None:
    payload = {
        "model": "openai/gpt-oss-20b",
        "messages": [{"role": "user", "content": "hello"}],
        "reasoning": {"effort": effort},
    }

    upstream = _locked_upstream_payload(
        payload,
        model="openai/gpt-oss-20b",
        max_tokens=256,
        bench_version=9,
    )

    assert upstream["reasoning"] == {"effort": effort, "exclude": True}


@pytest.mark.parametrize("effort", ["low", "medium", "high"])
def test_v9_broker_canonical_reasoning_block_is_idempotent(effort: str) -> None:
    payload = {
        "model": "openai/gpt-oss-20b",
        "messages": [{"role": "user", "content": "hello"}],
        "reasoning": {"effort": effort, "exclude": True},
    }

    upstream = _locked_upstream_payload(
        payload,
        model="openai/gpt-oss-20b",
        max_tokens=256,
        bench_version=9,
    )

    assert upstream["reasoning"] == {"effort": effort, "exclude": True}


@pytest.mark.parametrize("effort", ["low", "medium", "high"])
def test_v9_equal_reasoning_aliases_collapse_to_one_field(effort: str) -> None:
    payload = {
        "model": "openai/gpt-oss-20b",
        "messages": [{"role": "user", "content": "hello"}],
        "reasoning": {"effort": effort},
        "reasoning_effort": effort,
    }

    upstream = _locked_upstream_payload(
        payload,
        model="openai/gpt-oss-20b",
        max_tokens=256,
        bench_version=9,
    )

    assert upstream["reasoning"] == {"effort": effort, "exclude": True}
    assert "reasoning_effort" not in upstream


@pytest.mark.parametrize(
    ("nested", "flat"),
    [
        ("low", "medium"),
        ("low", "high"),
        ("medium", "low"),
        ("medium", "high"),
        ("high", "low"),
        ("high", "medium"),
    ],
)
def test_v9_conflicting_reasoning_aliases_prefer_nested(nested: str, flat: str) -> None:
    payload = {
        "model": "openai/gpt-oss-20b",
        "messages": [{"role": "user", "content": "hello"}],
        "reasoning": {"effort": nested},
        "reasoning_effort": flat,
    }

    upstream = _locked_upstream_payload(
        payload,
        model="openai/gpt-oss-20b",
        max_tokens=256,
        bench_version=9,
    )

    assert "reasoning_effort" not in upstream
    assert upstream["reasoning"] == {"effort": nested, "exclude": True}


@pytest.mark.parametrize(
    "value",
    [
        None,
        True,
        False,
        1,
        1.0,
        "",
        "LOW",
        " medium",
        "medium ",
        "none",
        "minimal",
        "xhigh",
    ],
)
def test_v9_invalid_flat_reasoning_effort_fails_closed(value: object) -> None:
    payload = {
        "model": "openai/gpt-oss-20b",
        "messages": [{"role": "user", "content": "hello"}],
        "reasoning_effort": value,
    }

    with pytest.raises(HTTPException) as raised:
        _locked_upstream_payload(
            payload,
            model="openai/gpt-oss-20b",
            max_tokens=256,
            bench_version=9,
        )

    assert raised.value.status_code == 400
    assert raised.value.detail == "invalid reasoning_effort"


@pytest.mark.parametrize(
    "value",
    [
        None,
        True,
        "low",
        [],
        {},
        {"effort": None},
        {"effort": True},
        {"effort": "none"},
        {"effort": "LOW"},
        {"effort": "medium", "exclude": False},
        {"effort": "medium", "enabled": True},
        {"effort": "medium", "max_tokens": 1},
        {"exclude": True},
    ],
)
def test_v9_invalid_or_provider_controlled_reasoning_object_fails_closed(
    value: object,
) -> None:
    payload = {
        "model": "openai/gpt-oss-20b",
        "messages": [{"role": "user", "content": "hello"}],
        "reasoning": value,
    }

    with pytest.raises(HTTPException) as raised:
        _locked_upstream_payload(
            payload,
            model="openai/gpt-oss-20b",
            max_tokens=256,
            bench_version=9,
        )

    assert raised.value.status_code == 400
    assert raised.value.detail in {"invalid reasoning", "invalid reasoning effort"}


@pytest.mark.parametrize("bench_version", [7, 8])
@pytest.mark.parametrize(
    "caller_reasoning",
    [
        {"reasoning": {"effort": "low"}},
        {"reasoning_effort": "high"},
        {"reasoning": {"effort": "high"}, "reasoning_effort": "low"},
        {"reasoning": {"enabled": False}},
    ],
)
def test_v7_v8_reasoning_contract_remains_fixed_medium(
    bench_version: int, caller_reasoning: dict[str, object]
) -> None:
    payload: dict[str, object] = {
        "model": "openai/gpt-oss-20b",
        "messages": [{"role": "user", "content": "hello"}],
        **caller_reasoning,
    }

    upstream = _locked_upstream_payload(
        payload,
        model="openai/gpt-oss-20b",
        max_tokens=256,
        bench_version=bench_version,
    )

    assert upstream["reasoning"] == {"effort": "medium", "exclude": True}
    assert "reasoning_effort" not in upstream


def test_identity_fields_are_accepted_and_never_reach_the_provider() -> None:
    """The only drops left: zero effect on the completion or on observation.

    Stripping is acceptable exactly when it changes neither what the model
    produces nor what the harness can see. These carry or fingerprint agent
    identity toward a third party under the pinned deny-retention posture and
    affect nothing about the answer, so dropping them cannot alter a result.
    """
    payload: dict[str, object] = {
        "model": "openai/gpt-oss-20b",
        "messages": [{"role": "user", "content": "hello"}],
        "user": "agent-7",
        "metadata": {"run": "abc"},
        "safety_identifier": "agent-7",
        "store": False,
        "stream_options": {"include_usage": True},
    }
    _validate_request_schema(payload)
    upstream = _locked_upstream_payload(
        payload, model="openai/gpt-oss-20b", max_tokens=256
    )
    for dropped in _DROPPED_REQUEST_FIELDS:
        assert dropped not in upstream, dropped


def test_grant_protecting_fields_are_pinned_not_forwarded() -> None:
    """The anti-cheat boundary, which does not loosen under default-forward."""
    payload: dict[str, object] = {
        "model": "attacker/model",
        "messages": [{"role": "user", "content": "hello"}],
        "n": 3,
        "best_of": 4,
        "reasoning": {"effort": "high"},
        "reasoning_effort": "high",
        "include_reasoning": True,
        "service_tier": "priority",
        "usage": {"include": True},
        "prompt_cache_key": "shared-bucket",
        "max_completion_tokens": 999_999,
    }
    _validate_request_schema(payload)
    upstream = _locked_upstream_payload(
        payload, model="openai/gpt-oss-20b", max_tokens=256
    )
    # Replaced with the ticket's values.
    assert upstream["model"] == "openai/gpt-oss-20b"
    assert upstream["n"] == 1
    assert upstream["max_tokens"] == 256
    assert upstream["reasoning"] == {"effort": "medium", "exclude": True}
    # Pinned by removal, so the platform's own request governs.
    for removed in (
        "best_of",
        "reasoning_effort",
        "include_reasoning",
        "service_tier",
        "usage",
        "prompt_cache_key",
        "max_completion_tokens",
    ):
        assert removed not in upstream, removed


def test_echoed_provider_tool_call_index_is_accepted_not_refused() -> None:
    """Grandmaster-style harnesses copy tool_calls back, including `index`.

    Live OpenRouter accepts the extra key, so the lock forwards it and only
    heals the proven-bad sibling ``reasoning_effort`` alias.
    """
    payload: dict[str, object] = {
        "model": "openai/gpt-oss-20b",
        "reasoning_effort": "medium",
        "messages": [
            {
                "role": "assistant",
                "content": "",
                "reasoning": "call the tool",
                "tool_calls": [
                    {
                        "id": "1",
                        "type": "function",
                        "index": 0,
                        "function": {"name": "search_memory", "arguments": "{}"},
                    }
                ],
            }
        ],
    }
    _validate_request_schema(payload)
    upstream = _locked_upstream_payload(
        payload, model="openai/gpt-oss-20b", max_tokens=256, bench_version=9
    )
    assert "reasoning_effort" not in upstream
    assert upstream["reasoning"] == {"effort": "medium", "exclude": True}
    message = upstream["messages"][0]
    assert message["reasoning"] == "call the tool"
    call = message["tool_calls"][0]
    assert call["index"] == 0
    assert call["function"] == {"name": "search_memory", "arguments": "{}"}


def test_echoed_assistant_reasoning_content_is_forwarded_not_stripped() -> None:
    """Crown/rig-core CompletionsClient recall emits ``reasoning_content``.

    Request-level ``reasoning`` is pinned to the ticket contract. Message-level
    traces are the agent's own history and must survive that pin. The lock
    still heals the flat ``reasoning_effort`` alias and strips tool-role
    ``name``.
    """
    payload: dict[str, object] = {
        "model": "openai/gpt-oss-20b",
        "reasoning_effort": "medium",
        "messages": [
            {
                "role": "assistant",
                "content": "",
                "reasoning_content": "I should call search_memory.",
                "reasoning": "I should call search_memory.",
                "reasoning_details": [
                    {
                        "type": "reasoning.text",
                        "text": "I should call search_memory.",
                    }
                ],
                "tool_calls": [
                    {
                        "id": "1",
                        "type": "function",
                        "index": 0,
                        "function": {"name": "search_memory", "arguments": "{}"},
                    }
                ],
            },
            {
                "role": "tool",
                "tool_call_id": "1",
                "name": "search_memory",
                "content": "ok",
            },
        ],
    }
    _validate_request_schema(payload)
    upstream = _locked_upstream_payload(
        payload, model="openai/gpt-oss-20b", max_tokens=256, bench_version=9
    )
    assert "reasoning_effort" not in upstream
    assert upstream["reasoning"] == {"effort": "medium", "exclude": True}
    message = upstream["messages"][0]
    assert message["reasoning_content"] == "I should call search_memory."
    assert message["reasoning"] == "I should call search_memory."
    assert message["reasoning_details"] == [
        {"type": "reasoning.text", "text": "I should call search_memory."}
    ]
    assert message["tool_calls"][0]["index"] == 0
    assert "name" not in upstream["messages"][1]


@pytest.mark.parametrize(
    ("field", "value", "detail"),
    [
        ("reasoning", {"effort": "medium"}, "invalid reasoning"),
        ("reasoning_content", {"text": "think"}, "invalid reasoning_content"),
        ("reasoning_details", "think", "invalid reasoning_details"),
    ],
)
def test_malformed_assistant_reasoning_traces_fail_closed(
    field: str, value: object, detail: str
) -> None:
    payload: dict[str, object] = {
        "model": "openai/gpt-oss-20b",
        "messages": [
            {"role": "assistant", "content": "", field: value},
        ],
    }
    with pytest.raises(HTTPException) as raised:
        _validate_request_schema(payload)
    assert raised.value.status_code == 400
    assert raised.value.detail == detail


def test_request_level_reasoning_pin_does_not_wipe_message_traces() -> None:
    payload: dict[str, object] = {
        "model": "openai/gpt-oss-20b",
        "reasoning": {"effort": "high"},
        "reasoning_effort": "low",
        "messages": [
            {
                "role": "assistant",
                "content": "ok",
                "reasoning_content": "keep this trace",
            }
        ],
    }
    _validate_request_schema(payload)
    upstream = _locked_upstream_payload(
        payload, model="openai/gpt-oss-20b", max_tokens=256, bench_version=9
    )
    assert "reasoning_effort" not in upstream
    assert upstream["reasoning"] == {"effort": "high", "exclude": True}
    assert upstream["messages"][0]["reasoning_content"] == "keep this trace"


def test_structured_outputs_and_logprobs_are_forwarded_intact() -> None:
    """Verified against the pinned v7 model on every supporting provider.

    `json_schema` conforms WITH the pinned reasoning block active, and OpenRouter
    routes around providers that lack `response_format` rather than failing, so
    forwarding cannot manufacture a 400. `logprobs` does not alter generation at
    all, so there was never an argument against it beyond the allowlist's
    silence.
    """
    schema = {
        "type": "json_schema",
        "json_schema": {
            "name": "answer",
            "strict": True,
            "schema": {
                "type": "object",
                "properties": {"color": {"type": "string"}},
                "required": ["color"],
                "additionalProperties": False,
            },
        },
    }
    payload: dict[str, object] = {
        "model": "openai/gpt-oss-20b",
        "messages": [{"role": "user", "content": "hello"}],
        "response_format": schema,
        "structured_outputs": True,
        "logprobs": True,
        "logit_bias": {"1234": -100},
        "prediction": {"type": "content", "content": "red"},
        "verbosity": "low",
    }
    _validate_request_schema(payload)
    upstream = _locked_upstream_payload(
        payload, model="openai/gpt-oss-20b", max_tokens=256
    )
    # Byte-identical, not merely present: a schema is only useful intact.
    assert upstream["response_format"] == schema
    assert upstream["structured_outputs"] is True
    assert upstream["logprobs"] is True
    assert upstream["logit_bias"] == {"1234": -100}
    assert upstream["prediction"] == {"type": "content", "content": "red"}
    assert upstream["verbosity"] == "low"


def test_forwarded_logprobs_are_actually_returned_to_the_caller() -> None:
    """Forwarding a field the response strips would be a silent no-op.

    The caller sets the flag, pays for the larger provider response, and
    receives nothing -- which is the bug this whole change exists to remove.
    """
    logprobs = {"content": [{"token": "hi", "logprob": -0.25, "top_logprobs": []}]}
    public = _public_provider_response(
        {
            "id": "gen-1",
            "object": "chat.completion",
            "created": 1_700_000_000,
            "model": "openai/gpt-oss-20b",
            "choices": [
                {
                    "index": 0,
                    "finish_reason": "stop",
                    "message": {"role": "assistant", "content": "hi"},
                    "logprobs": logprobs,
                }
            ],
            "usage": {"prompt_tokens": 1, "completion_tokens": 1},
        }
    )
    assert public["choices"][0]["logprobs"] == logprobs

    # A provider that does not support them returns null, which is the honest
    # answer rather than a fabricated one.
    without = _public_provider_response(
        {
            "id": "gen-2",
            "object": "chat.completion",
            "created": 1_700_000_000,
            "model": "openai/gpt-oss-20b",
            "choices": [
                {
                    "index": 0,
                    "finish_reason": "stop",
                    "message": {"role": "assistant", "content": "hi"},
                }
            ],
            "usage": {"prompt_tokens": 1, "completion_tokens": 1},
        }
    )
    assert without["choices"][0]["logprobs"] is None


def test_sampling_knobs_the_miner_owns_are_forwarded_unchanged() -> None:
    """Same class as temperature/top_p/seed, which this lane always forwarded.

    Dropping a deliberately-set sampling knob would silently change an agent's
    behaviour behind its back -- the exact failure mode this change removes.
    """
    payload = {
        "model": "openai/gpt-oss-20b",
        "messages": [{"role": "user", "content": "hello"}],
        "frequency_penalty": 0.5,
        "presence_penalty": -0.25,
        "temperature": 0.0,
        "seed": 42,
    }
    _validate_request_schema(payload)
    upstream = _locked_upstream_payload(
        payload, model="openai/gpt-oss-20b", max_tokens=256
    )
    assert upstream["frequency_penalty"] == 0.5
    assert upstream["presence_penalty"] == -0.25
    assert upstream["temperature"] == 0.0
    assert upstream["seed"] == 42


def test_unsupported_parameter_error_names_every_offending_key() -> None:
    """A miner must be able to discover which field broke the run."""
    with pytest.raises(HTTPException) as refused:
        _validate_request_schema(
            {
                "model": "openai/gpt-oss-20b",
                "messages": [{"role": "user", "content": "hello"}],
                "plugins": [{"id": "web"}],
                "models": ["attacker/model"],
            }
        )
    assert refused.value.status_code == 400
    detail = str(refused.value.detail)
    assert "unsupported inference parameter" in detail
    # Both keys named, sorted so the message is stable across dict ordering,
    # and each carrying the reason rather than a bare "unsupported".
    assert "models" in detail
    assert "plugins" in detail
    assert detail.index("models") < detail.index("plugins")
    assert "pinned by the ticket" in detail

    # A key nobody has classified still names itself.
    with pytest.raises(HTTPException) as novel:
        _validate_request_schema(
            {
                "model": "openai/gpt-oss-20b",
                "messages": [{"role": "user", "content": "hello"}],
                "some_future_openrouter_field": 1,
            }
        )
    assert "some_future_openrouter_field" in str(novel.value.detail)


def test_capability_limits_say_they_are_limits() -> None:
    """`stream` and `top_logprobs` are refusals we would rather not make.

    Both are capability limits of this lane rather than policy, so the message
    has to say which key and why -- a miner reading it should know whether to
    change their harness or ask us to change the platform.
    """
    with pytest.raises(HTTPException) as streaming:
        _validate_request_schema(
            {
                "model": "openai/gpt-oss-20b",
                "messages": [{"role": "user", "content": "hello"}],
                "stream": True,
            }
        )
    assert "stream" in str(streaming.value.detail)
    assert "non-streaming" in str(streaming.value.detail)

    with pytest.raises(HTTPException) as top:
        _validate_request_schema(
            {
                "model": "openai/gpt-oss-20b",
                "messages": [{"role": "user", "content": "hello"}],
                "logprobs": True,
                "top_logprobs": 5,
            }
        )
    detail = str(top.value.detail)
    assert "top_logprobs" in detail
    # Says the neighbouring field DOES work, so the miner keeps what they can.
    assert "logprobs is supported" in detail
    assert "response size" in detail


def test_every_field_has_exactly_one_decided_fate() -> None:
    """The partition, which is what makes default-forward reviewable.

    Under a forward-by-default posture the risk is no longer "a normal field is
    refused" but "a field that should have been pinned is forwarded by
    omission". These four sets must stay disjoint and must cover the surface.
    """
    assert (
        _PINNED_REQUEST_FIELDS | _DROPPED_REQUEST_FIELDS | _FORWARDED_REQUEST_FIELDS
        == _ALLOWED_REQUEST_FIELDS
    )
    for left, right in (
        (_PINNED_REQUEST_FIELDS, _DROPPED_REQUEST_FIELDS),
        (_PINNED_REQUEST_FIELDS, _FORWARDED_REQUEST_FIELDS),
        (_DROPPED_REQUEST_FIELDS, _FORWARDED_REQUEST_FIELDS),
    ):
        assert not left & right, sorted(left & right)
    # An accepted field can never also be a named refusal.
    assert not _ALLOWED_REQUEST_FIELDS & set(_REFUSED_REQUEST_FIELDS)
    # Every named refusal explains itself; "unsupported" alone is what we are
    # replacing, so an empty or lazy reason is a test failure.
    for key, reason in _REFUSED_REQUEST_FIELDS.items():
        assert reason and len(reason) > 12, key

    # The anti-cheat boundary, asserted by name. Anything that could buy
    # compute, a model, or an effort level the ticket did not grant must be
    # pinned or refused -- never forwarded.
    for lever in (
        "model",
        "n",
        "best_of",
        "reasoning",
        "reasoning_effort",
        "include_reasoning",
        "service_tier",
        "max_tokens",
        "max_completion_tokens",
    ):
        assert lever in _PINNED_REQUEST_FIELDS, lever
        assert lever not in _FORWARDED_REQUEST_FIELDS, lever
    for escape in ("models", "plugins", "transforms"):
        assert escape not in _ALLOWED_REQUEST_FIELDS
        assert escape in _REFUSED_REQUEST_FIELDS
    for routing_hint in ("provider", "route", "preset"):
        assert routing_hint in _DROPPED_REQUEST_FIELDS
        assert routing_hint not in _REFUSED_REQUEST_FIELDS

    # The fields the operator explicitly asked to be usable by harnesses.
    for normal in (
        "seed",
        "logprobs",
        "response_format",
        "logit_bias",
        "frequency_penalty",
        "presence_penalty",
        "stop",
        "top_k",
    ):
        assert normal in _FORWARDED_REQUEST_FIELDS, normal


def test_caller_shape_rejections_do_not_cool_shared_provider_route() -> None:
    assert not _provider_rejection_is_route_observable(400)
    assert not _provider_rejection_is_route_observable(422)
    for status_code in (401, 402, 403, 404, 408, 409, 429, 500, 503):
        assert _provider_rejection_is_route_observable(status_code)


def test_router_metadata_provider_is_trusted_but_never_returned_to_harness() -> None:
    upstream = {
        "id": "gen-test",
        "object": "chat.completion",
        "created": 1,
        "model": "openai/gpt-oss-20b",
        "provider": "legacy-provider-must-not-leak",
        "system_fingerprint": "provider-specific-fingerprint",
        "service_tier": "provider-specific-tier",
        "openrouter_metadata": {
            "summary": "selected Groq",
            "endpoints": {
                "available": [
                    {
                        "provider": "Groq",
                        "model": "openai/gpt-oss-20b",
                        "selected": True,
                    }
                ]
            },
        },
        "choices": [
            {
                "index": 0,
                "finish_reason": "tool_calls",
                "native_finish_reason": "provider-native-finish",
                "message": {
                    "role": "assistant",
                    "content": None,
                    "tool_calls": [
                        {
                            "id": "call-1",
                            "type": "function",
                            "function": {"name": "lookup", "arguments": "{}"},
                            "provider_extension": "must-not-leak",
                        }
                    ],
                    "provider_extension": "must-not-leak",
                },
            }
        ],
        "usage": {
            "prompt_tokens": 3,
            "completion_tokens": 4,
            "total_tokens": 7,
            "cost": 0.01,
            "cost_details": {"upstream_inference_cost": 0.009},
            "is_byok": False,
        },
    }

    assert _upstream_provider(upstream) == "Groq"
    public = _public_provider_response(upstream)
    encoded = str(public)
    for secret in (
        "Groq",
        "legacy-provider",
        "provider-specific",
        "must-not-leak",
        "openrouter_metadata",
        "system_fingerprint",
        "service_tier",
        "native_finish_reason",
        "cost",
    ):
        assert secret not in encoded
    assert public["usage"] == {
        "prompt_tokens": 3,
        "completion_tokens": 4,
        "total_tokens": 7,
    }
    assert public["choices"][0]["message"]["tool_calls"][0] == {
        "id": "call-1",
        "type": "function",
        "function": {"name": "lookup", "arguments": "{}"},
    }


def test_router_metadata_rejects_ambiguous_selected_provider() -> None:
    with pytest.raises(HTTPException):
        _upstream_provider(
            {
                "openrouter_metadata": {
                    "endpoints": {
                        "available": [
                            {"provider": "Groq", "selected": True},
                            {"provider": "Together", "selected": True},
                        ]
                    }
                }
            }
        )


def test_provider_choice_error_metadata_is_not_deliverable() -> None:
    with pytest.raises(HTTPException):
        _public_provider_response(
            {
                "id": "gen-test",
                "object": "chat.completion",
                "created": 1,
                "model": "openai/gpt-oss-20b",
                "choices": [
                    {
                        "index": 0,
                        "finish_reason": "stop",
                        "message": {"role": "assistant", "content": None},
                        "error": {
                            "message": "Groq raw error",
                            "metadata": {"provider": "Groq"},
                        },
                    }
                ],
                "usage": {"prompt_tokens": 3, "completion_tokens": 0},
            }
        )


def test_adaptive_route_remains_exact_and_disables_fallback() -> None:
    assert _provider_preferences(
        routing_mode="adaptive",
        provider="Groq",
        quantization="fp8",
    ) == {
        "only": ["Groq"],
        "quantizations": ["fp8"],
        "allow_fallbacks": False,
        "data_collection": "deny",
        "zdr": True,
    }


def test_exchange_response_serializes_budget_evidence_in_json() -> None:
    encoded = InferenceExchangeResponse(
        grant_id=uuid4(),
        bearer="b" * 32,
        proxy_url="https://platform.test/api/v1/inference/chat/completions",
        expires_at=datetime.now(UTC),
        generation=1,
        request_budget=8192,
        token_budget=75_000_000,
        embedding_request_budget=100_000,
        embedding_token_budget=1_000_000_000,
        max_output_tokens=8192,
    ).model_dump(mode="json")
    assert encoded["request_budget"] == 8192
    assert encoded["token_budget"] == 75_000_000
    assert encoded["embedding_request_budget"] == 100_000
    assert encoded["embedding_token_budget"] == 1_000_000_000
    assert encoded["max_output_tokens"] == 8192


def test_legacy_offer_omits_additive_v7_route_identity() -> None:
    offer = InferenceGrantOffer(
        grant_id=uuid4(),
        exchange_url="https://platform.test/api/v1/inference/exchange",
        proxy_url="https://platform.test/api/v1/inference/chat/completions",
        allowed_models=["qwen/qwen3-32b"],
        request_budget=10,
        token_budget=100,
        expires_at=datetime.now(UTC),
    )
    encoded = offer.model_dump(mode="json")
    assert "provider" not in encoded
    assert "profile_revision" not in encoded


class _FakeGrant:
    """Minimal stand-in for the ORM row the proxy loads after proof verification."""

    def __init__(self, *, bench_version: int, allowed_models: list[str]) -> None:
        self.bench_version = bench_version
        self.allowed_models = allowed_models
        self.grant_id = uuid4()
        self.agent_id = uuid4()
        self.slot_id = "slot-0"


class _FakeConfig:
    allowed_models = ("qwen/qwen3-32b", "openai/gpt-oss-20b")


def test_v7_serves_the_ticket_model_regardless_of_what_the_agent_asked_for() -> None:
    """A miner controls every byte of the request body; the ticket is authoritative.

    Pre-v7 harnesses default DITTOBENCH_MODEL to qwen/qwen3-32b, which is in the
    globally permitted set, so without this the agent — not the ticket — picked
    the scored model.
    """
    grant = _FakeGrant(bench_version=7, allowed_models=["openai/gpt-oss-20b"])
    resolved = _locked_grant_model(
        grant, requested="qwen/qwen3-32b", config=_FakeConfig()
    )
    assert resolved == "openai/gpt-oss-20b"


def test_v7_matching_request_resolves_to_the_same_locked_model() -> None:
    grant = _FakeGrant(bench_version=7, allowed_models=["openai/gpt-oss-20b"])
    assert (
        _locked_grant_model(grant, requested="openai/gpt-oss-20b", config=_FakeConfig())
        == "openai/gpt-oss-20b"
    )


def test_v7_model_mismatch_is_recorded_as_an_evasion_signal(caplog) -> None:
    grant = _FakeGrant(bench_version=7, allowed_models=["openai/gpt-oss-20b"])
    with caplog.at_level("WARNING"):
        _locked_grant_model(grant, requested="qwen/qwen3-32b", config=_FakeConfig())
    assert any(
        "model mismatch" in record.getMessage()
        and "qwen/qwen3-32b" in record.getMessage()
        and "openai/gpt-oss-20b" in record.getMessage()
        for record in caplog.records
    )


def test_v7_grant_without_a_pinned_model_fails_closed() -> None:
    grant = _FakeGrant(bench_version=7, allowed_models=[])
    with pytest.raises(HTTPException) as excinfo:
        _locked_grant_model(grant, requested="openai/gpt-oss-20b", config=_FakeConfig())
    assert excinfo.value.status_code == 409


def test_pre_v7_model_selection_semantics_are_unchanged() -> None:
    """Historical replay must not move: the caller still chooses from the set."""
    grant = _FakeGrant(bench_version=6, allowed_models=["openai/gpt-oss-20b"])
    assert (
        _locked_grant_model(grant, requested="qwen/qwen3-32b", config=_FakeConfig())
        == "qwen/qwen3-32b"
    )
    with pytest.raises(HTTPException) as excinfo:
        _locked_grant_model(grant, requested="anthropic/claude", config=_FakeConfig())
    assert excinfo.value.status_code == 403


def test_embedding_vectors_pass_through_without_per_element_validation() -> None:
    """Vector contents are forwarded as parsed; the envelope is still checked.

    Per-element float validation cost 42us per 768-dim vector on the shared
    event loop, and the trusted broker re-validates every element for NaN/Inf
    before any harness sees it (inference_broker.go:1144-1151). Dropping the
    duplicate check must not weaken anything that is O(vectors) rather than
    O(floats): arity, ordering, vector length, model identity, and usage all
    still fail closed below.
    """
    model = "perplexity/pplx-embed-v1-0.6b"

    # A payload that the old per-element validator would have rejected and the
    # passthrough forwards verbatim. This is the behavior change, stated
    # explicitly so a future reader sees it was chosen, not overlooked.
    exotic = [float("nan"), float("inf"), -float("inf")] + [0.5] * 765
    public, prompt_tokens = _public_embedding_response(
        {
            "object": "list",
            "model": model,
            "data": [{"object": "embedding", "index": 0, "embedding": exotic}],
            "usage": {"prompt_tokens": 3, "total_tokens": 3},
        },
        model=model,
        dimensions=768,
        input_count=1,
    )
    assert prompt_tokens == 3
    assert public["data"][0]["embedding"] is exotic

    # Everything cheap stays enforced.
    vector = [0.0] * 768
    for broken in (
        # wrong arity vs the request
        {
            "model": model,
            "data": [{"object": "embedding", "index": 0, "embedding": vector}],
            "usage": {"prompt_tokens": 1, "total_tokens": 1},
        },
        # out-of-order / misaligned index
        {
            "model": model,
            "data": [
                {"object": "embedding", "index": 1, "embedding": vector},
                {"object": "embedding", "index": 0, "embedding": vector},
            ],
            "usage": {"prompt_tokens": 1, "total_tokens": 1},
        },
        # truncated vector: length is still validated, only elements are not
        {
            "model": model,
            "data": [
                {"object": "embedding", "index": 0, "embedding": [0.0] * 512},
                {"object": "embedding", "index": 1, "embedding": vector},
            ],
            "usage": {"prompt_tokens": 1, "total_tokens": 1},
        },
        # a vector that is not a list at all
        {
            "model": model,
            "data": [
                {"object": "embedding", "index": 0, "embedding": "not-a-vector"},
                {"object": "embedding", "index": 1, "embedding": vector},
            ],
            "usage": {"prompt_tokens": 1, "total_tokens": 1},
        },
        # missing usage: metering depends on it, so it must still fail closed
        {
            "model": model,
            "data": [
                {"object": "embedding", "index": 0, "embedding": vector},
                {"object": "embedding", "index": 1, "embedding": vector},
            ],
        },
    ):
        with pytest.raises(HTTPException, match="invalid provider response"):
            _public_embedding_response(
                broken, model=model, dimensions=768, input_count=2
            )


def test_embedding_model_mismatch_still_refuses_rather_than_substituting() -> None:
    """#428's request-side policy is unchanged by the response passthrough.

    Chat substitutes the ticket model; embeddings refuse, because silently
    rewriting the model would change the vector space under a caller that is
    validating dimensions. The two paths differ on purpose, and the response
    passthrough must not disturb that: the guarantee lives on the request side.
    """
    model = "perplexity/pplx-embed-v1-0.6b"
    payload = {
        "model": "embeddinggemma",
        "input": ["one"],
        "dimensions": 768,
        "encoding_format": "float",
    }
    with pytest.raises(HTTPException) as refused:
        _validated_embedding_payload(payload, model=model, dimensions=768)
    assert refused.value.status_code == 400


def test_the_reservation_is_a_token_estimate_not_a_byte_count() -> None:
    """The over-reservation, measured on a body the size v7 actually sends.

    ``max_tokens + len(body)`` treated byte length as a token count. It is a
    true upper bound, but roughly 4x the truth for this lane's JSON, and it was
    not merely held: every reclamation path books it as ``prompt_tokens``.
    """
    body = (
        b'{"model":"openai/gpt-oss-20b","messages":[' + b'{"role":"user"}' * 600 + b"]"
    )
    assert 8_000 < len(body) < 10_000

    # The prompt half is where the over-estimate lived, and it is 4x to within
    # the rounding of one ceiling division.
    assert abs(len(body) / _estimated_tokens(body) - 4) < 0.01

    # On the whole reservation the effect is ~3x rather than 4x, because the
    # permitted output (``max_tokens``) was never the inflated part.
    old_reservation = 1024 + len(body)
    new_reservation = 1024 + _estimated_tokens(body)
    assert 3.0 < old_reservation / new_reservation < 3.2

    # And a v7-sized body lands in the right neighbourhood of the ~2,300 tokens
    # per chat call the calibration fleet actually measures.
    assert 1_500 < _estimated_tokens(body) < 3_000


def test_the_charge_ceiling_stays_a_true_bound() -> None:
    """The estimate may be wrong; the ceiling may not.

    ``max_chargeable_tokens`` is what bounds untrusted provider accounting, so
    it has to stay tokenizer-independent -- a token cannot consume less than one
    byte of the UTF-8 body. Keeping it above the estimate is what stops the
    clamp from marking ordinary token-dense calls non-deliverable.
    """
    for body in (b"x", b"{}", b"a" * 4096, "ü".encode() * 2048):
        ceiling = _max_chargeable_tokens(body, output_tokens=1024)
        assert ceiling >= len(body)
        # Never below the reservation, so the clamp can only ever be looser
        # than the estimate -- which is what keeps a legitimate call deliverable.
        assert ceiling >= 1024 + _estimated_tokens(body)
    # Never zero, so the `reserved_tokens > 0` check constraint always holds.
    assert _max_chargeable_tokens(b"") >= 1
    assert _estimated_tokens(b"") == 1
