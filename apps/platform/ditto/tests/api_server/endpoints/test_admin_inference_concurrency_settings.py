"""Contract tests for the hosted-embedding concurrency board."""

from collections.abc import AsyncIterator
from dataclasses import replace

import httpx
import pytest
from fastapi import FastAPI
from sqlalchemy.ext.asyncio import (
    AsyncSession,
    async_sessionmaker,
)

from ditto.api_server.dependencies import get_session
from ditto.api_server.inference_concurrency_settings import (
    InferenceConcurrencySettingsResolver,
)

pytestmark = pytest.mark.asyncio

_ADMIN_TOKEN = "test-admin-token-at-least-32-characters"
_HEADERS = {"Authorization": f"Bearer {_ADMIN_TOKEN}"}
_URL = "/api/v1/admin/inference-concurrency-settings"
_CONFIRMATION = "APPLY INFERENCE CONCURRENCY SETTINGS"


@pytest.fixture
def settings_maker(
    session_maker: async_sessionmaker[AsyncSession],
) -> async_sessionmaker[AsyncSession]:
    """Alias onto the root real-Postgres fixture in ``ditto/tests/conftest.py``.

    Aliasing rather than renaming keeps every test signature in this file
    untouched, so the diff cannot change what is asserted.
    """
    return session_maker


def _install(app: FastAPI, maker: async_sessionmaker[AsyncSession]) -> None:
    app.state.config = replace(app.state.config, admin_api_token=_ADMIN_TOKEN)
    app.state.session_maker = maker
    app.state.inference_concurrency_settings = InferenceConcurrencySettingsResolver()

    async def _session() -> AsyncIterator[AsyncSession]:
        async with maker() as session:
            yield session

    app.dependency_overrides[get_session] = _session


def _settings(**overrides: object) -> dict[str, object]:
    """A COMPLETE policy body; the board stores the whole object."""
    settings: dict[str, object] = {
        "chat_request_budget": 8192,
        "chat_token_budget": 25_000_000,
        "chat_per_ticket_concurrency": 16,
        "chat_per_validator_concurrency": 48,
        "chat_global_concurrency": 96,
        "embedding_per_ticket_concurrency": 12,
        "embedding_per_validator_concurrency": 48,
        "embedding_global_concurrency": 96,
        "chat_per_ticket_requests_per_minute": 1920,
        "chat_per_validator_requests_per_minute": 7680,
        "chat_global_requests_per_minute": 23040,
        "embedding_per_ticket_requests_per_minute": 10_000,
        "embedding_per_validator_requests_per_minute": 40_000,
        "embedding_global_requests_per_minute": 100_000,
    }
    settings.update(overrides)
    return settings


def _payload(
    *,
    expected_revision: int = 0,
    confirmation: str = _CONFIRMATION,
    settings: dict[str, object] | None = None,
    **setting_overrides: object,
) -> dict[str, object]:
    return {
        "scope": "*",
        "expected_revision": expected_revision,
        "settings": settings
        if settings is not None
        else _settings(**setting_overrides),
        "reason": "widen the hosted v7 embedding lane; ollama rationale is gone",
        "actor": "backroom:test",
        "confirmation": confirmation,
    }


class TestAuth:
    async def test_read_requires_the_admin_token(
        self,
        app: FastAPI,
        client: httpx.AsyncClient,
        settings_maker: async_sessionmaker[AsyncSession],
    ) -> None:
        _install(app, settings_maker)
        assert (await client.get(_URL)).status_code == 401


class TestWrite:
    async def test_apply_then_read_back(
        self,
        app: FastAPI,
        client: httpx.AsyncClient,
        settings_maker: async_sessionmaker[AsyncSession],
    ) -> None:
        _install(app, settings_maker)
        created = await client.post(
            _URL, json=_payload(embedding_per_ticket_concurrency=24), headers=_HEADERS
        )
        assert created.status_code == 200, created.text
        assert created.json()["revision"] == 1

        body = (await client.get(_URL, headers=_HEADERS)).json()
        assert body["effective"]["source"] == "revision"
        assert body["effective"]["settings"]["embedding_per_ticket_concurrency"] == 24
        assert len(body["history"]) == 1

    async def test_legacy_write_preserves_current_v10_runtime_policy(
        self,
        app: FastAPI,
        client: httpx.AsyncClient,
        settings_maker: async_sessionmaker[AsyncSession],
    ) -> None:
        _install(app, settings_maker)
        runtime = {
            "case_concurrency": 4,
            "relay_delay_fingerprint_mode": "shadow",
            "relay_delay_fingerprint_min_ms": 40,
            "relay_delay_fingerprint_max_ms": 160,
            # A non-default cap proves a legacy write carries it forward.
            "sandbox_memory_limit_mib": 8_192,
        }
        first = await client.post(
            _URL,
            json=_payload(benchmark_runtime=runtime),
            headers=_HEADERS,
        )
        assert first.status_code == 200, first.text

        # Simulate an already-deployed Backroom that knows only the original
        # eight fields. It may change those fields, but it must not reset the
        # newer runtime controls during a rolling upgrade.
        legacy = await client.post(
            _URL,
            json=_payload(
                expected_revision=1,
                embedding_per_ticket_concurrency=10,
            ),
            headers=_HEADERS,
        )
        assert legacy.status_code == 200, legacy.text
        assert legacy.json()["settings"]["benchmark_runtime"] == runtime

    async def test_wrong_confirmation_is_refused(
        self,
        app: FastAPI,
        client: httpx.AsyncClient,
        settings_maker: async_sessionmaker[AsyncSession],
    ) -> None:
        _install(app, settings_maker)
        response = await client.post(
            _URL, json=_payload(confirmation="yes"), headers=_HEADERS
        )
        assert response.status_code == 409

    async def test_stale_expected_revision_is_refused(
        self,
        app: FastAPI,
        client: httpx.AsyncClient,
        settings_maker: async_sessionmaker[AsyncSession],
    ) -> None:
        _install(app, settings_maker)
        assert (await client.post(_URL, json=_payload(), headers=_HEADERS)).status_code
        stale = await client.post(
            _URL, json=_payload(expected_revision=0), headers=_HEADERS
        )
        assert stale.status_code == 409

    async def test_write_invalidates_the_admission_cache(
        self,
        app: FastAPI,
        client: httpx.AsyncClient,
        settings_maker: async_sessionmaker[AsyncSession],
    ) -> None:
        """A backroom write must land on the next admission, not in five seconds.

        The resolver is TTL-cached so the admission path does not pay a SELECT
        per embedding. Without this invalidation an operator pulling the brake
        would watch nothing happen and pull it again.
        """
        _install(app, settings_maker)
        resolver = app.state.inference_concurrency_settings
        assert (
            await resolver.resolve(settings_maker)
        ).embedding_per_ticket_concurrency == 12
        await client.post(
            _URL, json=_payload(embedding_per_ticket_concurrency=2), headers=_HEADERS
        )
        resolved = await resolver.resolve(settings_maker)
        assert resolved.embedding_per_ticket_concurrency == 2
