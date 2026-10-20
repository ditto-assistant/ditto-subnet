"""Pre-reservation inference refusals stay queryable without the prompt."""

from __future__ import annotations

import json
from collections.abc import AsyncIterator
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from uuid import uuid4

import httpx
import pytest
from fastapi import FastAPI
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from ditto.api_server.dependencies import get_session
from ditto.db.models import InferenceAdmissionRejection, InferenceRequest

_TOKEN = "test-admin-token-at-least-32-characters"
_SECRET = "SECRET_PROMPT_do_not_store"


def _headers(grant_id, *, when: datetime) -> dict[str, str]:
    return {
        "X-Ditto-Grant": str(grant_id),
        "X-Ditto-Generation": "1",
        "X-Ditto-Nonce": str(uuid4()),
        "X-Ditto-Requested-At": when.isoformat(),
        "X-Ditto-Proof": "not-a-real-proof",
        "Authorization": "Bearer not-a-token",
    }


def _enable(
    app: FastAPI, maker: async_sessionmaker[AsyncSession], *, limit: int
) -> None:
    app.state.session_maker = maker

    async def _session() -> AsyncIterator[AsyncSession]:
        async with maker() as session:
            yield session

    app.dependency_overrides[get_session] = _session
    app.state.config = replace(
        app.state.config,
        admin_api_token=_TOKEN,
        inference_proxy=replace(
            app.state.config.inference_proxy,
            enabled=True,
            openrouter_api_key="test-only",
            request_body_bytes=limit,
        ),
    )


@pytest.mark.asyncio
async def test_size_schema_and_stale_refusals_are_stored_without_the_prompt(
    app: FastAPI,
    client: httpx.AsyncClient,
    session_maker: async_sessionmaker[AsyncSession],
) -> None:
    _enable(app, session_maker, limit=16)
    grant_id = uuid4()
    oversized = await client.post(
        "/api/v1/inference/chat/completions",
        content=_SECRET.encode(),
        headers=_headers(grant_id, when=datetime.now(UTC)),
    )
    assert oversized.status_code == 413

    _enable(app, session_maker, limit=1 << 20)
    invalid = await client.post(
        "/api/v1/inference/chat/completions",
        content=b"not-json SECRET_PROMPT_do_not_store",
        headers=_headers(grant_id, when=datetime.now(UTC)),
    )
    assert invalid.status_code == 400
    stale = await client.post(
        "/api/v1/inference/chat/completions",
        content=b"{}",
        headers=_headers(grant_id, when=datetime.now(UTC) - timedelta(minutes=5)),
    )
    assert stale.status_code == 409

    async with session_maker() as session:
        rows = list(
            await session.scalars(
                select(InferenceAdmissionRejection).where(
                    InferenceAdmissionRejection.grant_id == grant_id
                )
            )
        )
        requests = await session.scalar(
            select(func.count()).select_from(InferenceRequest)
        )
    assert requests == 0
    by_code = {row.admission_code: row for row in rows}
    assert set(by_code) == {"request_too_large", "invalid_json", "stale_session"}
    assert by_code["request_too_large"].http_status == 413
    assert by_code["request_too_large"].byte_limit == 16
    assert by_code["request_too_large"].request_bytes == len(_SECRET)
    assert by_code["invalid_json"].http_status == 400
    assert by_code["stale_session"].http_status == 409
    stored = " ".join(
        " ".join(str(value) for value in row.__dict__.values()) for row in rows
    )
    assert _SECRET not in stored
    assert "Bearer" not in stored

    listed = await client.get(
        "/api/v1/admin/inference-admission-rejections",
        params={"grant_id": str(grant_id)},
        headers={"Authorization": f"Bearer {_TOKEN}"},
    )
    assert listed.status_code == 200, listed.text
    body = listed.json()
    assert body["counts"]["request_too_large"] == 1
    assert _SECRET not in listed.text


@pytest.mark.asyncio
@pytest.mark.parametrize("bad_field", ["model", "dimensions", "input"])
async def test_embedding_schema_refusal_is_stored_without_input(
    bad_field: str,
    app: FastAPI,
    client: httpx.AsyncClient,
    session_maker: async_sessionmaker[AsyncSession],
) -> None:
    _enable(app, session_maker, limit=1 << 20)
    grant_id = uuid4()
    config = app.state.config.inference_proxy
    payload = {
        "model": config.embedding_model,
        "input": [_SECRET],
        "dimensions": config.embedding_dimensions,
        "encoding_format": "float",
    }
    payload[bad_field] = {
        "model": "wrong-model",
        "dimensions": config.embedding_dimensions + 1,
        "input": [_SECRET, ""],
    }[bad_field]
    body = json.dumps(payload).encode()
    rejected = await client.post(
        "/api/v1/inference/embeddings",
        content=body,
        headers=_headers(grant_id, when=datetime.now(UTC)),
    )
    assert rejected.status_code == 400
    assert rejected.json()["message"] == "invalid embedding request"

    async with session_maker() as session:
        rows = list(
            await session.scalars(
                select(InferenceAdmissionRejection).where(
                    InferenceAdmissionRejection.grant_id == grant_id
                )
            )
        )
        requests = await session.scalar(
            select(func.count()).select_from(InferenceRequest)
        )
    assert requests == 0
    assert len(rows) == 1
    row = rows[0]
    assert (row.lane, row.http_status, row.admission_code) == (
        "embedding",
        400,
        "invalid_schema",
    )
    assert row.request_bytes == len(body)
    assert row.byte_limit is None
    assert row.validator_hotkey is None
    stored = " ".join(str(value) for value in row.__dict__.values())
    assert _SECRET not in stored
    assert "wrong-model" not in stored
