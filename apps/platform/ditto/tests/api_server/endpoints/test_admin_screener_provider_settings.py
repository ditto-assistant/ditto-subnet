"""Audited Backroom control for screener and builder provider routing."""

from collections.abc import AsyncIterator
from dataclasses import replace

import httpx
import pytest
from fastapi import FastAPI
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from ditto.api_server.dependencies import get_session

pytestmark = pytest.mark.asyncio

_ADMIN_TOKEN = "test-admin-token-at-least-32-characters"
_HEADERS = {"Authorization": f"Bearer {_ADMIN_TOKEN}"}
_PATH = "/api/v1/admin/screener-provider-settings"


def _install(app: FastAPI, maker: async_sessionmaker[AsyncSession]) -> None:
    app.state.config = replace(app.state.config, admin_api_token=_ADMIN_TOKEN)

    async def _session() -> AsyncIterator[AsyncSession]:
        async with maker() as session:
            yield session

    app.dependency_overrides[get_session] = _session


def _payload(
    *,
    expected_revision: int,
    screening: list[str],
    builds: list[str],
    confirmation: str | None = None,
) -> dict[str, object]:
    settings = {
        "runtime_provider_priority": screening,
        "source_review_provider_priority": screening,
        "build_provider_priority": builds,
    }
    phrase = (
        f"APPLY SCREENER PROVIDERS BUILDS={'>'.join(builds)} "
        f"RUNTIME={'>'.join(screening)} SOURCE_REVIEW={'>'.join(screening)}"
    )
    return {
        "environment": "prod",
        "expected_revision": expected_revision,
        "settings": settings,
        "reason": "Route around scheduled Targon provider maintenance",
        "actor": "operator@example.com",
        "confirmation": confirmation if confirmation is not None else phrase,
    }


async def test_provider_settings_are_atomic_audited_and_cas_guarded(
    app: FastAPI,
    client: httpx.AsyncClient,
    session_maker: async_sessionmaker[AsyncSession],
) -> None:
    _install(app, session_maker)
    initial = await client.get(_PATH, headers=_HEADERS)
    assert initial.status_code == 200, initial.text
    assert initial.json()["current"]["revision"] == 0
    assert initial.json()["current"]["settings"] == {
        "runtime_provider_priority": ["targon", "gcp"],
        "source_review_provider_priority": ["targon", "gcp"],
        "build_provider_priority": ["targon", "gcp"],
    }

    applied = await client.post(
        _PATH,
        headers=_HEADERS,
        json=_payload(
            expected_revision=0,
            screening=["gcp", "targon"],
            builds=["gcp"],
        ),
    )
    assert applied.status_code == 200, applied.text
    revision = applied.json()["revision"]

    capacity = await client.get("/api/v1/admin/screener-capacity", headers=_HEADERS)
    assert capacity.status_code == 200, capacity.text
    control = capacity.json()["provider_control"]
    assert control["current"]["revision"] == revision
    assert control["current"]["settings"]["build_provider_priority"] == ["gcp"]

    stale = await client.post(
        _PATH,
        headers=_HEADERS,
        json=_payload(
            expected_revision=0,
            screening=["targon", "gcp"],
            builds=["targon", "gcp"],
        ),
    )
    assert stale.status_code == 409


async def test_provider_settings_retain_gcp_and_exact_confirmation(
    app: FastAPI,
    client: httpx.AsyncClient,
    session_maker: async_sessionmaker[AsyncSession],
) -> None:
    _install(app, session_maker)
    missing_fallback = await client.post(
        _PATH,
        headers=_HEADERS,
        json=_payload(
            expected_revision=0,
            screening=["targon"],
            builds=["targon"],
        ),
    )
    assert missing_fallback.status_code == 422

    wrong_confirmation = await client.post(
        _PATH,
        headers=_HEADERS,
        json=_payload(
            expected_revision=0,
            screening=["gcp"],
            builds=["gcp"],
            confirmation="APPLY",
        ),
    )
    assert wrong_confirmation.status_code == 409
