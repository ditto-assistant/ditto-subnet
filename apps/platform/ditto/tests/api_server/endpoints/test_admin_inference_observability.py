from __future__ import annotations

from collections.abc import AsyncIterator
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock, MagicMock
from uuid import UUID

import httpx
import pytest
from fastapi import FastAPI
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from ditto.api_models.inference_observability import RuntimeProfileArtifact
from ditto.api_server.dependencies import get_session

pytestmark = pytest.mark.asyncio
_ADMIN_TOKEN = "test-admin-token-at-least-32-characters"
_HEADERS = {
    "Authorization": f"Bearer {_ADMIN_TOKEN}",
    "X-Admin-Actor": "operator@example.com",
}
_PROFILE_ID = UUID("11111111-1111-4111-8111-111111111111")


def _artifact() -> RuntimeProfileArtifact:
    created = datetime.now(UTC)
    return RuntimeProfileArtifact(
        profile_id=_PROFILE_ID,
        target="platform-relay-1",
        profile_type="cpu",
        seconds=15,
        source_revision="a" * 40,
        checked_out_revision="a" * 40,
        revision_drift=False,
        actor="operator@example.com",
        reason="investigate slow benchmark runs",
        created_at=created,
        expires_at=created + timedelta(minutes=15),
        byte_size=5,
        sha256="b" * 64,
        filename="relay-cpu.pb.gz",
        download_path=f"/api/v1/admin/runtime-profiles/{_PROFILE_ID}/download",
    )


def _install(app: FastAPI) -> MagicMock:
    app.state.config = replace(app.state.config, admin_api_token=_ADMIN_TOKEN)
    store = MagicMock()
    store.capture = AsyncMock(return_value=_artifact())
    store.get.return_value = _artifact()
    app.state.runtime_profiles = store
    return store


def _capture_payload(
    confirmation: str = "CAPTURE RUNTIME PROFILE",
) -> dict[str, object]:
    return {
        "target": "platform-relay-1",
        "profile_type": "cpu",
        "seconds": 15,
        "reason": "investigate slow benchmark runs",
        "confirmation": confirmation,
    }


async def test_capture_requires_admin_token(
    app: FastAPI, client: httpx.AsyncClient
) -> None:
    _install(app)
    response = await client.post(
        "/api/v1/admin/runtime-profiles",
        json=_capture_payload(),
        headers={"X-Admin-Actor": "operator@example.com"},
    )
    assert response.status_code == 401


async def test_capture_requires_exact_confirmation_and_actor(
    app: FastAPI, client: httpx.AsyncClient
) -> None:
    store = _install(app)
    wrong = await client.post(
        "/api/v1/admin/runtime-profiles",
        json=_capture_payload("yes"),
        headers=_HEADERS,
    )
    assert wrong.status_code == 409
    no_actor = await client.post(
        "/api/v1/admin/runtime-profiles",
        json=_capture_payload(),
        headers={"Authorization": f"Bearer {_ADMIN_TOKEN}"},
    )
    assert no_actor.status_code == 422
    store.capture.assert_not_awaited()


async def test_capture_passes_signed_in_actor_to_private_store(
    app: FastAPI, client: httpx.AsyncClient
) -> None:
    store = _install(app)
    response = await client.post(
        "/api/v1/admin/runtime-profiles",
        json=_capture_payload(),
        headers=_HEADERS,
    )
    assert response.status_code == 200, response.text
    store.capture.assert_awaited_once_with(
        target="platform-relay-1",
        profile_type="cpu",
        seconds=15,
        actor="operator@example.com",
        reason="investigate slow benchmark runs",
    )


async def test_download_requires_actor_even_behind_admin_token(
    app: FastAPI, client: httpx.AsyncClient
) -> None:
    _install(app)
    response = await client.get(
        f"/api/v1/admin/runtime-profiles/{_PROFILE_ID}/download",
        headers={"Authorization": f"Bearer {_ADMIN_TOKEN}"},
    )
    assert response.status_code == 422


def _install_session(
    app: FastAPI, session_maker: async_sessionmaker[AsyncSession]
) -> None:
    async def _session() -> AsyncIterator[AsyncSession]:
        async with session_maker() as session:
            yield session

    app.dependency_overrides[get_session] = _session


async def test_runtime_metrics_reads_the_ledger_and_keeps_its_shape(
    app: FastAPI,
    client: httpx.AsyncClient,
    session_maker: async_sessionmaker[AsyncSession],
) -> None:
    """The Backroom contract, end to end against a real ledger.

    The relay probes hit loopback ports nothing listens on in a test and must
    come back ``unavailable`` rather than take the endpoint down with them.
    """
    _install(app)
    _install_session(app, session_maker)
    response = await client.get(
        "/api/v1/admin/inference-runtime-metrics", headers=_HEADERS
    )
    assert response.status_code == 200, response.text
    body = response.json()
    assert set(body) == {
        "observed_at",
        "settings_revision",
        "settings_checksum",
        "lanes",
        "windows",
        "relays",
    }
    assert [lane["request_kind"] for lane in body["lanes"]] == ["chat", "embedding"]
    for lane in body["lanes"]:
        assert set(lane) >= {
            "active_requests",
            "live_grants",
            "stale_started_requests",
            "per_ticket_limit",
            "per_validator_limit",
            "global_limit",
            "peak_per_ticket_concurrency_60m",
            "peak_per_validator_concurrency_60m",
            "peak_global_concurrency_60m",
        }
    # An empty hour has no window rows; the shape is a list either way.
    assert body["windows"] == []
    assert [relay["target"] for relay in body["relays"]] == [
        "platform-relay-1",
        "platform-relay-2",
    ]
    assert {relay["status"] for relay in body["relays"]} == {"unavailable"}
