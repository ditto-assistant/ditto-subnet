"""End-to-end proof that a caller-supplied ``reasoning`` effort is pinned, not refused.

This is the regression test for the incident that motivated the schema change.
``Cooking`` (rank 15 on v1) added one line in v2 that sent
``reasoning: {"effort": "medium"}`` on every chat call. ``reasoning`` was absent
from the request allowlist, so the gate answered 400 forty-three lines *before*
``begin_inference_request`` -- no ``inference_requests`` row, nothing in
telemetry, and an error body that never named the field. Roughly 81 of ~480 chat
calls per run died that way, the run failed v7's complete-usage check, and the
miner burned three submissions on it.

The irony that decided the fix: ``benchmark_reasoning`` already stamps
``{"effort": "medium", "exclude": True}`` on every v7 request. The miner was
rejected for redundantly asking for precisely what they were already being
given.

So the property under test is not "``reasoning`` is allowed". It is the stronger
one that makes allowing it safe: the caller's value is *discarded and replaced*,
so a request asking for ``high`` is served, metered, and billed as ``medium``.
The anti-cheat property and the availability fix are the same mechanism.
"""

from __future__ import annotations

import base64
import json
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import UUID, uuid4

import httpx
import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from fastapi import HTTPException, Response
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker

from ditto.api_models.agent_status import AgentStatus
from ditto.api_models.ticket_status import TicketStatus
from ditto.api_server.config import InferenceProxyConfig
from ditto.api_server.endpoints.inference import _proxy_message, proxy_chat_completions
from ditto.api_server.inference_routing import (
    AGGREGATE_CALIBRATION_SAMPLES,
    AGGREGATE_PROVIDER,
    V7_MODEL,
    aggregate_profile_revision,
)
from ditto.db.models import (
    Agent,
    InferenceGrant,
    InferenceProviderRoute,
    InferenceRequest,
    InferenceRoutingPolicy,
    ValidatorTicket,
)
from ditto.db.queries.inference import (
    activate_inference_grant,
    ensure_inference_grant,
)

pytestmark = pytest.mark.integration

_MAX_OUTPUT_TOKENS = 1024


def _config() -> InferenceProxyConfig:
    return InferenceProxyConfig(
        enabled=True,
        required=False,
        public_base_url="https://platform.example",
        openrouter_api_key="test-key",
        upstream_url="https://openrouter.ai/api/v1/chat/completions",
        allowed_models=(V7_MODEL,),
        provider="openrouter",
        routing_mode="aggregate_throughput",
        request_budget=1000,
        token_budget=1_000_000,
        embedding_upstream_url="https://openrouter.ai/api/v1/embeddings",
        embedding_model="perplexity/pplx-embed-v1-0.6b",
        embedding_profile="dittobench-v7-openrouter-pplx-embed-v1-0.6b-768-v1",
        embedding_provider="Perplexity",
        embedding_dimensions=768,
        embedding_request_budget=100_000,
        embedding_token_budget=1_000_000_000,
        embedding_per_ticket_concurrency=64,
        embedding_per_validator_concurrency=256,
        embedding_global_concurrency=1024,
        embedding_per_ticket_requests_per_minute=10_000,
        embedding_per_validator_requests_per_minute=40_000,
        embedding_global_requests_per_minute=100_000,
        embedding_request_body_bytes=1 << 20,
        embedding_response_body_bytes=16 << 20,
        per_ticket_concurrency=64,
        per_validator_concurrency=256,
        global_concurrency=1024,
        per_ticket_requests_per_minute=10_000,
        per_validator_requests_per_minute=40_000,
        global_requests_per_minute=100_000,
        request_body_bytes=256 << 10,
        response_body_bytes=1 << 20,
        timeout_seconds=10,
        max_output_tokens=_MAX_OUTPUT_TOKENS,
    )


class _State:
    """The three attributes ``proxy_chat_completions`` reads off ``app.state``."""

    def __init__(self, *, config: Any, session_maker: Any, client: httpx.AsyncClient):
        class _Config:
            inference_proxy = config

        self.config = _Config()
        self.session_maker = session_maker
        self.inference_client = client


class _App:
    def __init__(self, state: _State) -> None:
        self.state = state


async def _seed_grant(
    maker: Any,
    *,
    config: InferenceProxyConfig,
    public_key: str,
    bench_version: int = 7,
) -> tuple[UUID, str, int]:
    """One agent, ticket, and grant minted through the real routed path."""
    now = datetime.now(UTC)
    async with maker() as session, session.begin():
        # A calibrated aggregate route, so ``select_route`` admits the v7 mint
        # exactly as it does in production rather than being bypassed.
        session.add(
            InferenceRoutingPolicy(
                model=V7_MODEL,
                enabled=True,
                speed_weight=1.0,
                cost_weight=0.0,
                exploration_weight=0.0,
                exploration_ticket_budget=0,
                min_tool_accuracy=0.0,
                min_composite=0.0,
                min_calibration_samples=1,
                max_error_rate=1.0,
                max_timeout_rate=1.0,
                cooldown_seconds=1,
                ewma_alpha=0.3,
                updated_at=now,
            )
        )
        session.add(
            InferenceProviderRoute(
                model=V7_MODEL,
                provider=AGGREGATE_PROVIDER,
                profile_revision=aggregate_profile_revision(
                    V7_MODEL, bench_version=bench_version
                ),
                status="healthy",
                calibration_status="eligible",
                calibration_tool_accuracy=1.0,
                calibration_composite=1.0,
                calibration_sample_count=AGGREGATE_CALIBRATION_SAMPLES,
                calibration_manifest_sha256="0" * 64,
                discovered_at=now,
                ewma_error_rate=0.0,
                ewma_timeout_rate=0.0,
                sample_count=0,
                selected_ticket_count=0,
                exploration_ticket_count=0,
                updated_at=now,
            )
        )
        await session.flush()
        agent = Agent(
            agent_id=uuid4(),
            miner_hotkey="5FRZRm3R6ESJ4TtxaQ51vxk99hdkFdchUHFZVNSfYAUbehyR",
            name="reasoning-pin",
            sha256=uuid4().hex + uuid4().hex,
            status=AgentStatus.EVALUATING,
            created_at=now,
        )
        ticket = ValidatorTicket(
            agent_id=agent.agent_id,
            validator_hotkey="validator-reasoning",
            slot_id="slot-0",
            status=TicketStatus.ISSUED,
            issued_at=now,
            deadline=now + timedelta(minutes=20),
            bench_version=bench_version,
            attempt_count=1,
        )
        session.add_all([agent, ticket])
        await session.flush()
        grant = await ensure_inference_grant(session, ticket=ticket, config=config)
        assert grant is not None
        activated = await activate_inference_grant(
            session,
            grant_id=grant.grant_id,
            validator_hotkey="validator-reasoning",
            broker_public_key=public_key,
            now=now,
            config=config,
        )
        assert activated is not None
        live = activated[0]
        # Minted through the real v7 path, so these are the platform's own
        # values rather than anything this test arranged.
        assert live.bench_version == bench_version
        assert live.allowed_models == [V7_MODEL]
        assert live.route_provider == AGGREGATE_PROVIDER
        return live.grant_id, activated[1], live.generation


def _signed_request(
    *,
    app: _App,
    grant_id: UUID,
    bearer: str,
    generation: int,
    private: Ed25519PrivateKey,
    body: bytes,
) -> dict[str, Any]:
    """Header kwargs for one authenticated proxy call over ``body``."""
    from starlette.requests import Request

    nonce = uuid4()
    requested_at = datetime.now(UTC)
    proof = private.sign(
        _proxy_message(
            grant_id=grant_id,
            generation=generation,
            nonce=nonce,
            requested_at=requested_at,
            body=body,
        )
    )

    async def receive() -> dict[str, Any]:
        return {"type": "http.request", "body": body, "more_body": False}

    request = Request(
        {
            "type": "http",
            "method": "POST",
            "path": "/api/v1/inference/chat/completions",
            "headers": [],
            "app": app,
        },
        receive,
    )
    return {
        "request": request,
        "x_ditto_grant": grant_id,
        "x_ditto_generation": generation,
        "x_ditto_nonce": nonce,
        "x_ditto_requested_at": requested_at,
        "x_ditto_proof": base64.urlsafe_b64encode(proof).decode().rstrip("="),
        "authorization": f"Bearer {bearer}",
    }


@pytest.mark.asyncio
async def test_caller_reasoning_effort_is_accepted_pinned_to_medium_and_charged(
    session_maker: async_sessionmaker[Any],
) -> None:
    config = _config()
    private = Ed25519PrivateKey.generate()
    public = private.public_key().public_bytes(
        encoding=serialization.Encoding.Raw,
        format=serialization.PublicFormat.Raw,
    )
    grant_id, bearer, generation = await _seed_grant(
        session_maker,
        config=config,
        public_key=base64.urlsafe_b64encode(public).decode().rstrip("="),
    )

    seen: list[dict[str, Any]] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        seen.append(json.loads(request.content))
        return httpx.Response(
            200,
            json={
                "id": "gen-1",
                "object": "chat.completion",
                "created": 1_700_000_000,
                "model": V7_MODEL,
                # Opt-in router metadata; ``_upstream_provider`` requires exactly
                # one selected endpoint for trusted route telemetry.
                "openrouter_metadata": {
                    "endpoints": {
                        "available": [{"provider": "Fireworks", "selected": True}]
                    }
                },
                "choices": [
                    {
                        "index": 0,
                        "finish_reason": "stop",
                        "message": {"role": "assistant", "content": "ok"},
                    }
                ],
                "usage": {
                    "prompt_tokens": 31,
                    "completion_tokens": 17,
                    "total_tokens": 48,
                    # Aggregate routing derives trusted cost from the provider's
                    # own figure; without it the call books as usage-unavailable.
                    "cost": 0.000123,
                },
            },
        )

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    app = _App(_State(config=config, session_maker=session_maker, client=client))

    # Exactly the shape `Cooking` v2/v3 emits, but asking for the *strongest*
    # effort rather than the pinned one -- the adversarial direction.
    body = json.dumps(
        {
            "model": V7_MODEL,
            "messages": [{"role": "user", "content": "hello"}],
            "temperature": 0.0,
            "seed": 42,
            "reasoning": {"effort": "high"},
        }
    ).encode()

    try:
        response = await proxy_chat_completions(
            **_signed_request(
                app=app,
                grant_id=grant_id,
                bearer=bearer,
                generation=generation,
                private=private,
                body=body,
            )
        )
    finally:
        await client.aclose()

    # 1. Accepted. This is the byte-for-byte request that used to 400.
    assert response.status_code == 200

    # 2. Reached the provider with the *pinned* value, not the caller's "high".
    assert len(seen) == 1
    assert seen[0]["reasoning"] == {"effort": "medium", "exclude": True}
    assert seen[0]["model"] == V7_MODEL
    assert seen[0]["max_tokens"] == _MAX_OUTPUT_TOKENS
    assert seen[0]["n"] == 1
    assert seen[0]["stream"] is False
    # The caller's own sampling choices survive untouched.
    assert seen[0]["temperature"] == 0.0
    assert seen[0]["seed"] == 42

    # 3. Charged, and charged as the medium call that actually ran. The request
    #    reached the ledger at all -- the old rejection never wrote a row, which
    #    is why this failure mode was invisible in telemetry.
    async with session_maker() as session:
        row = (
            await session.scalars(
                select(InferenceRequest).where(InferenceRequest.grant_id == grant_id)
            )
        ).one()
        assert row.status == "completed"
        assert row.model == V7_MODEL
        assert row.prompt_tokens == 31
        assert row.completion_tokens == 17
        grant = await session.get(InferenceGrant, grant_id)
        assert grant is not None
        assert grant.request_count == 1
        assert grant.prompt_tokens == 31
        assert grant.completion_tokens == 17
        assert grant.cost_microusd == 123


@pytest.mark.asyncio
async def test_v9_reasoning_strategy_reaches_provider_and_invalid_aliases_spend_nothing(
    session_maker: async_sessionmaker[Any],
) -> None:
    """The authenticated provider boundary applies the complete V9 contract."""
    config = _config()
    private = Ed25519PrivateKey.generate()
    public = private.public_key().public_bytes(
        encoding=serialization.Encoding.Raw,
        format=serialization.PublicFormat.Raw,
    )
    grant_id, bearer, generation = await _seed_grant(
        session_maker,
        config=config,
        public_key=base64.urlsafe_b64encode(public).decode().rstrip("="),
        bench_version=9,
    )
    seen: list[dict[str, Any]] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        seen.append(json.loads(request.content))
        return httpx.Response(
            200,
            json={
                "id": f"gen-{len(seen)}",
                "object": "chat.completion",
                "created": 1_700_000_000,
                "model": V7_MODEL,
                "openrouter_metadata": {
                    "endpoints": {
                        "available": [{"provider": "Fireworks", "selected": True}]
                    }
                },
                "choices": [
                    {
                        "index": 0,
                        "finish_reason": "stop",
                        "message": {"role": "assistant", "content": "ok"},
                    }
                ],
                "usage": {
                    "prompt_tokens": 10,
                    "completion_tokens": 5,
                    "total_tokens": 15,
                    "cost": 0.000001,
                },
            },
        )

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    app = _App(_State(config=config, session_maker=session_maker, client=client))

    async def submit(payload: dict[str, Any]) -> Response:
        body = json.dumps(payload).encode()
        return await proxy_chat_completions(
            **_signed_request(
                app=app,
                grant_id=grant_id,
                bearer=bearer,
                generation=generation,
                private=private,
                body=body,
            )
        )

    try:
        selected = await submit(
            {
                "model": V7_MODEL,
                "messages": [{"role": "user", "content": "choose low"}],
                "reasoning_effort": "low",
            }
        )
        defaulted = await submit(
            {
                "model": V7_MODEL,
                "messages": [{"role": "user", "content": "use the default"}],
            }
        )
        broker_canonical = await submit(
            {
                "model": V7_MODEL,
                "messages": [{"role": "user", "content": "broker canonical"}],
                "reasoning": {"effort": "high", "exclude": True},
            }
        )
        with pytest.raises(HTTPException) as conflicting:
            await submit(
                {
                    "model": V7_MODEL,
                    "messages": [{"role": "user", "content": "conflict"}],
                    "reasoning": {"effort": "high"},
                    "reasoning_effort": "low",
                }
            )
    finally:
        await client.aclose()

    assert selected.status_code == 200
    assert defaulted.status_code == 200
    assert broker_canonical.status_code == 200
    assert conflicting.value.status_code == 400
    assert conflicting.value.detail == "conflicting reasoning effort"
    assert [request["reasoning"] for request in seen] == [
        {"effort": "low", "exclude": True},
        {"effort": "medium", "exclude": True},
        {"effort": "high", "exclude": True},
    ]
    assert all("reasoning_effort" not in request for request in seen)

    async with session_maker() as session:
        requests = list(
            await session.scalars(
                select(InferenceRequest).where(
                    InferenceRequest.grant_id == grant_id
                )
            )
        )
        assert len(requests) == 3
        assert all(request.status == "completed" for request in requests)
        grant = await session.get(InferenceGrant, grant_id)
        assert grant is not None
        assert grant.request_count == 3
        assert grant.prompt_tokens == 30
        assert grant.completion_tokens == 15


@pytest.mark.asyncio
async def test_structured_outputs_reach_the_provider_and_return_unmangled(
    session_maker: async_sessionmaker[Any],
) -> None:
    """`response_format` is forwarded byte-identical and its answer survives.

    Verified upstream before forwarding: against the pinned v7 model, a
    `json_schema` request conforms on every supporting provider WITH the pinned
    reasoning block active, and OpenRouter routes around the one provider that
    lacks `response_format` rather than failing the request. So forwarding
    cannot manufacture the live 400 that would have made this worse than the
    silent strip it replaces.

    Both halves matter. A schema that arrives mutated is useless, and a
    conforming answer that the response allowlist mangles on the way back is the
    same silent failure wearing a different hat.
    """
    config = _config()
    private = Ed25519PrivateKey.generate()
    public = private.public_key().public_bytes(
        encoding=serialization.Encoding.Raw,
        format=serialization.PublicFormat.Raw,
    )
    grant_id, bearer, generation = await _seed_grant(
        session_maker,
        config=config,
        public_key=base64.urlsafe_b64encode(public).decode().rstrip("="),
    )

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
    seen: list[dict[str, Any]] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        seen.append(json.loads(request.content))
        return httpx.Response(
            200,
            json={
                "id": "gen-1",
                "object": "chat.completion",
                "created": 1_700_000_000,
                "model": V7_MODEL,
                "openrouter_metadata": {
                    "endpoints": {
                        "available": [{"provider": "Fireworks", "selected": True}]
                    }
                },
                "choices": [
                    {
                        "index": 0,
                        "finish_reason": "stop",
                        "message": {
                            "role": "assistant",
                            "content": '{"color":"blue"}',
                        },
                        "logprobs": {
                            "content": [
                                {"token": "blue", "logprob": -0.5, "top_logprobs": []}
                            ]
                        },
                    }
                ],
                "usage": {
                    "prompt_tokens": 12,
                    "completion_tokens": 6,
                    "total_tokens": 18,
                    "cost": 0.000045,
                },
            },
        )

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    app = _App(_State(config=config, session_maker=session_maker, client=client))
    body = json.dumps(
        {
            "model": V7_MODEL,
            "messages": [{"role": "user", "content": "Name one color."}],
            "response_format": schema,
            "logprobs": True,
            "logit_bias": {"1234": -100},
            "frequency_penalty": 0.25,
            "seed": 42,
        }
    ).encode()

    try:
        response = await proxy_chat_completions(
            **_signed_request(
                app=app,
                grant_id=grant_id,
                bearer=bearer,
                generation=generation,
                private=private,
                body=body,
            )
        )
    finally:
        await client.aclose()

    assert response.status_code == 200
    # Reached the provider byte-identical -- a mutated schema is a broken one.
    assert seen[0]["response_format"] == schema
    assert seen[0]["logprobs"] is True
    assert seen[0]["logit_bias"] == {"1234": -100}
    assert seen[0]["frequency_penalty"] == 0.25
    assert seen[0]["seed"] == 42
    # Still pinned alongside it: structured outputs buy no reasoning effort.
    assert seen[0]["reasoning"] == {"effort": "medium", "exclude": True}
    assert seen[0]["model"] == V7_MODEL

    # And the answer survives the response allowlist unmangled, logprobs
    # included -- forwarding a request field whose response is stripped would
    # be a silent no-op.
    returned = json.loads(bytes(response.body))
    choice = returned["choices"][0]
    assert choice["message"]["content"] == '{"color":"blue"}'
    assert json.loads(choice["message"]["content"]) == {"color": "blue"}
    assert choice["logprobs"]["content"][0]["token"] == "blue"


@pytest.mark.asyncio
async def test_unknown_request_field_names_itself_in_the_error(
    session_maker: async_sessionmaker[Any],
) -> None:
    """The legibility backstop: a refusal must say which key it refused.

    Whatever the allowlist admits, something is eventually refused, and the
    miner's only channel is the harness's stderr. ``Cooking`` could not discover
    ``reasoning`` was the problem because this string used to be the bare
    ``"unsupported inference parameter"``.
    """
    config = _config()
    private = Ed25519PrivateKey.generate()
    public = private.public_key().public_bytes(
        encoding=serialization.Encoding.Raw,
        format=serialization.PublicFormat.Raw,
    )
    grant_id, bearer, generation = await _seed_grant(
        session_maker,
        config=config,
        public_key=base64.urlsafe_b64encode(public).decode().rstrip("="),
    )

    async def handler(_: httpx.Request) -> httpx.Response:  # pragma: no cover
        raise AssertionError("refused requests must never reach the provider")

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    app = _App(_State(config=config, session_maker=session_maker, client=client))
    body = json.dumps(
        {
            "model": V7_MODEL,
            "messages": [{"role": "user", "content": "hello"}],
            "plugins": [{"id": "web"}],
            "transforms": ["middle-out"],
        }
    ).encode()

    from fastapi import HTTPException

    try:
        with pytest.raises(HTTPException) as refused:
            await proxy_chat_completions(
                **_signed_request(
                    app=app,
                    grant_id=grant_id,
                    bearer=bearer,
                    generation=generation,
                    private=private,
                    body=body,
                )
            )
    finally:
        await client.aclose()

    assert refused.value.status_code == 400
    detail = str(refused.value.detail)
    assert "plugins" in detail
    assert "transforms" in detail
