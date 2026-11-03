"""Python proxy mirror of the #3271 cost ceiling, end to end on the endpoint.

The Go relay is the production request plane; this pins that the still-mounted
Python embedding route books ledger cost at the stamped price, carries
``X-Ditto-Ledger-Cost-Microusd`` when a ceiling is live, and answers 4110 once
an enforced ceiling is spent.
"""

from __future__ import annotations

import base64
import json
from dataclasses import replace
from typing import Any

import httpx
import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from sqlalchemy.ext.asyncio import async_sessionmaker

from ditto.api_server.endpoints.inference import (
    InferenceDeclinedError,
    proxy_embeddings,
)
from ditto.db.models import InferenceGrant
from ditto.db.queries.inference import InferenceDecline
from ditto.tests.integration.test_inference_reasoning_integration import (
    _App,
    _config,
    _seed_grant,
    _signed_request,
    _State,
)


def _embedding_handler(dimensions: int, tokens: int):
    async def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "object": "list",
                "model": "pplx-embed-v1-0.6b",
                "data": [
                    {"object": "embedding", "index": 0, "embedding": [0.0] * dimensions}
                ],
                "usage": {"prompt_tokens": tokens, "total_tokens": tokens},
            },
        )

    return handler


async def _embed(app: _App, *, grant_id, bearer, generation, private, config) -> Any:
    body = json.dumps(
        {
            "model": config.embedding_model,
            "input": ["cost ceiling"],
            "dimensions": config.embedding_dimensions,
            "encoding_format": "float",
        }
    ).encode()
    return await proxy_embeddings(
        **_signed_request(
            app=app,
            grant_id=grant_id,
            bearer=bearer,
            generation=generation,
            private=private,
            body=body,
        )
    )


async def _live(
    session_maker: async_sessionmaker[Any], *, ceiling: int | None, mode: str
):
    config = replace(
        _config(), run_cost_ceiling_microusd=ceiling, cost_ceiling_mode=mode
    )
    private = Ed25519PrivateKey.generate()
    public = private.public_key().public_bytes(
        encoding=serialization.Encoding.Raw, format=serialization.PublicFormat.Raw
    )
    grant_id, bearer, generation = await _seed_grant(
        session_maker,
        config=config,
        public_key=base64.urlsafe_b64encode(public).decode().rstrip("="),
    )
    async with session_maker() as session, session.begin():
        grant = await session.get(InferenceGrant, grant_id)
        assert grant is not None
        # 2 micro-USD per token: a 5-token embedding books 10.
        grant.embedding_price_microusd_per_token = 2.0
    return config, private, grant_id, bearer, generation


@pytest.mark.asyncio
async def test_off_mode_books_ledger_without_a_header(
    session_maker: async_sessionmaker[Any],
) -> None:
    config, private, grant_id, bearer, generation = await _live(
        session_maker, ceiling=None, mode="off"
    )
    client = httpx.AsyncClient(
        transport=httpx.MockTransport(
            _embedding_handler(config.embedding_dimensions, 5)
        )
    )
    app = _App(_State(config=config, session_maker=session_maker, client=client))
    try:
        response = await _embed(
            app,
            grant_id=grant_id,
            bearer=bearer,
            generation=generation,
            private=private,
            config=config,
        )
    finally:
        await client.aclose()
    assert response.status_code == 200
    assert "X-Ditto-Ledger-Cost-Microusd" not in response.headers
    async with session_maker() as session:
        grant = await session.get(InferenceGrant, grant_id)
        assert grant is not None
        assert grant.ledger_cost_microusd == 10
        assert grant.embedding_cost_microusd == 10  # stamped catalog price


@pytest.mark.asyncio
async def test_enforced_ceiling_header_then_4110(
    session_maker: async_sessionmaker[Any],
) -> None:
    config, private, grant_id, bearer, generation = await _live(
        session_maker, ceiling=8, mode="enforce"
    )
    client = httpx.AsyncClient(
        transport=httpx.MockTransport(
            _embedding_handler(config.embedding_dimensions, 5)
        )
    )
    app = _App(_State(config=config, session_maker=session_maker, client=client))
    kwargs = {
        "grant_id": grant_id,
        "bearer": bearer,
        "generation": generation,
        "private": private,
        "config": config,
    }
    try:
        # 0 < 8: admitted; this call crosses (books 10) and still finishes.
        crossing = await _embed(app, **kwargs)
        assert crossing.status_code == 200
        assert crossing.headers["X-Ditto-Ledger-Cost-Microusd"] == "10"
        with pytest.raises(InferenceDeclinedError) as declined:
            await _embed(app, **kwargs)
    finally:
        await client.aclose()
    assert declined.value.decline is InferenceDecline.COST_CEILING_EXHAUSTED
    assert declined.value.headers == {"X-Ditto-Ledger-Cost-Microusd": "10"}
    async with session_maker() as session:
        grant = await session.get(InferenceGrant, grant_id)
        assert grant is not None
        assert grant.status == "exhausted"
