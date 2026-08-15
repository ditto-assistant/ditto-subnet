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

    async def test_write_requires_the_admin_token(
        self,
        app: FastAPI,
        client: httpx.AsyncClient,
        settings_maker: async_sessionmaker[AsyncSession],
    ) -> None:
        _install(app, settings_maker)
        assert (await client.post(_URL, json=_payload())).status_code == 401


class TestRead:
    async def test_empty_board_reports_the_shipped_defaults_as_effective(
        self,
        app: FastAPI,
        client: httpx.AsyncClient,
        settings_maker: async_sessionmaker[AsyncSession],
    ) -> None:
        _install(app, settings_maker)
        body = (await client.get(_URL, headers=_HEADERS)).json()
        assert body["current"] == []
        assert body["effective"]["source"] == "default"
        assert body["effective"]["revision"] == 0
        # The claim under test: with no revision written, the fleet is already
        # running the raised limits rather than the vestigial 1/8/32 -- and,
        # since this board grew a chat field, the raised request budget rather
        # than the 1024 that was exhausting legitimate agents.
        assert body["effective"]["settings"] == {
            "chat_request_budget": 8192,
            "chat_token_budget": 25_000_000,
            "chat_per_ticket_concurrency": 16,
            "chat_per_validator_concurrency": 48,
            "chat_global_concurrency": 96,
            "embedding_per_ticket_concurrency": 12,
            "embedding_per_validator_concurrency": 48,
            "embedding_global_concurrency": 96,
        }


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

    async def test_emergency_brake_down_is_accepted(
        self,
        app: FastAPI,
        client: httpx.AsyncClient,
        settings_maker: async_sessionmaker[AsyncSession],
    ) -> None:
        """Reverting must be as easy as ramping.

        A board an operator can only raise is not a safety lever. The admission
        path turns a lowered limit into ``503`` backpressure rather than a lost
        lease, which is what makes this safe to do mid-run.
        """
        _install(app, settings_maker)
        response = await client.post(
            _URL,
            json=_payload(
                embedding_per_ticket_concurrency=1,
                embedding_per_validator_concurrency=1,
                embedding_global_concurrency=1,
            ),
            headers=_HEADERS,
        )
        assert response.status_code == 200, response.text

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

    async def test_partial_policy_is_refused(
        self,
        app: FastAPI,
        client: httpx.AsyncClient,
        settings_maker: async_sessionmaker[AsyncSession],
    ) -> None:
        _install(app, settings_maker)
        response = await client.post(
            _URL,
            json=_payload(settings={"embedding_global_concurrency": 96}),
            headers=_HEADERS,
        )
        # The envelope middleware replaces the body with a generic message, so
        # the status is all this layer can assert; the named-missing-fields text
        # is pinned in ditto/tests/api_models/test_inference_concurrency_settings.
        assert response.status_code == 422

    async def test_inverted_hierarchy_is_refused(
        self,
        app: FastAPI,
        client: httpx.AsyncClient,
        settings_maker: async_sessionmaker[AsyncSession],
    ) -> None:
        _install(app, settings_maker)
        response = await client.post(
            _URL,
            json=_payload(
                embedding_per_ticket_concurrency=64,
                embedding_per_validator_concurrency=8,
                embedding_global_concurrency=96,
            ),
            headers=_HEADERS,
        )
        assert response.status_code == 422

    async def test_above_the_boot_check_ceiling_is_refused(
        self,
        app: FastAPI,
        client: httpx.AsyncClient,
        settings_maker: async_sessionmaker[AsyncSession],
    ) -> None:
        """Never accept a revision that ``check_config`` would reject at boot."""
        _install(app, settings_maker)
        response = await client.post(
            _URL,
            json=_payload(
                embedding_per_ticket_concurrency=513,
                embedding_per_validator_concurrency=513,
                embedding_global_concurrency=513,
            ),
            headers=_HEADERS,
        )
        assert response.status_code == 422

    async def test_non_global_scope_is_refused(
        self,
        app: FastAPI,
        client: httpx.AsyncClient,
        settings_maker: async_sessionmaker[AsyncSession],
    ) -> None:
        _install(app, settings_maker)
        payload = _payload()
        payload["scope"] = "validator-1"
        response = await client.post(_URL, json=payload, headers=_HEADERS)
        assert response.status_code == 422

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
