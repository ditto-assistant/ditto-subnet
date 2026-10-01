"""Backroom read of the subnet liveness endpoint (ditto-subnet#2600)."""

from __future__ import annotations

import hashlib
from collections.abc import AsyncIterator
from dataclasses import replace
from datetime import UTC, datetime, timedelta

import httpx
import pytest
from fastapi import FastAPI
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from ditto.api_server.dependencies import get_session
from ditto.db.models import (
    ScreenerNode,
    ScreenerNodeChannelSettingsRevision,
    SourceEmissionCollectorCursor,
)

pytestmark = pytest.mark.asyncio
_ADMIN_TOKEN = "test-admin-token-at-least-32-characters"
_HEADERS = {"Authorization": f"Bearer {_ADMIN_TOKEN}"}
_URL = "/api/v1/admin/subnet-liveness"


def _install(app: FastAPI, session_maker: async_sessionmaker[AsyncSession]) -> None:
    app.state.config = replace(app.state.config, admin_api_token=_ADMIN_TOKEN)

    async def _session() -> AsyncIterator[AsyncSession]:
        async with session_maker() as session:
            yield session

    app.dependency_overrides[get_session] = _session


async def test_read_requires_the_admin_token(
    app: FastAPI,
    client: httpx.AsyncClient,
    session_maker: async_sessionmaker[AsyncSession],
) -> None:
    _install(app, session_maker)
    assert (await client.get(_URL)).status_code == 401
    assert (
        await client.get(_URL, headers={"Authorization": "Bearer wrong-token"})
    ).status_code == 401


async def test_rejects_an_invalid_environment(
    app: FastAPI,
    client: httpx.AsyncClient,
    session_maker: async_sessionmaker[AsyncSession],
) -> None:
    _install(app, session_maker)
    response = await client.get(f"{_URL}?environment=Prod;drop", headers=_HEADERS)
    assert response.status_code == 422
    # A whole-deployment read must not be labelled as another environment.
    response = await client.get(f"{_URL}?environment=staging", headers=_HEADERS)
    assert response.status_code == 422


async def test_reports_every_signal_without_secrets(
    app: FastAPI,
    client: httpx.AsyncClient,
    session_maker: async_sessionmaker[AsyncSession],
) -> None:
    _install(app, session_maker)
    now = datetime.now(UTC)
    node_token_hash = hashlib.sha256(b"node-bearer-token").hexdigest()
    provider_secret = "provider-marker-not-for-responses"
    async with session_maker() as session, session.begin():
        session.add(
            ScreenerNode(
                environment="prod",
                node_id="subnet-screener-1",
                provider="hetzner",
                provider_resource_id="test-resource-1",
                screener_hotkey="5NodeHotkeyXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXX",
                token_hash=node_token_hash,
                previous_token_hash=hashlib.sha256(b"old-token").hexdigest(),
                token_expires_at=now + timedelta(hours=1),
                status="active",
                capacity=2,
            )
        )
        session.add(
            SourceEmissionCollectorCursor(
                netuid=app.state.config.chain.netuid,
                block=10,
                block_hash="0x" + "2" * 64,
                updated_at=now - timedelta(minutes=1),
                last_blocked_reason=(
                    f"ConnectionError: wss://rpc.example/{provider_secret}"
                ),
            )
        )
    async with session_maker() as session, session.begin():
        session.add(
            ScreenerNodeChannelSettingsRevision(
                environment="prod",
                node_id="subnet-screener-1",
                parent_revision=0,
                settings={"screening_concurrency": 1},
                reason="liveness endpoint test",
                actor="operator@example.com",
            )
        )

    response = await client.get(_URL, headers=_HEADERS)
    assert response.status_code == 200, response.text
    body = response.json()
    assert set(body) == {
        "generated_at",
        "environment",
        "status",
        "signals",
        "unavailable",
    }
    assert body["environment"] == "prod"
    assert [signal["name"] for signal in body["signals"]] == [
        "screening_admission",
        "oldest_claimable_upload",
        "scoring_throughput",
        "v13_scorer_cohort_pin",
        "oldest_actionable_hold",
        "lease_overrun",
        "source_emission_collector",
    ]
    for signal in body["signals"]:
        assert set(signal) == {
            "name",
            "status",
            "value",
            "unit",
            "warn_threshold",
            "threshold",
            "since",
            "hint",
            "detail",
        }
        assert signal["status"] in {"ok", "warn", "breach"}
        assert signal["threshold"] > 0
    collector = body["signals"][-1]
    assert collector["detail"]["blocked_reason_class"] == "ConnectionError"
    raw = response.text
    for secret in (
        _ADMIN_TOKEN,
        node_token_hash,
        provider_secret,
        "rpc.example",
        "operator@example.com",
    ):
        assert secret not in raw
