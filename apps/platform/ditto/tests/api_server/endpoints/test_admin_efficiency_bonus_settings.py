"""Contract, auth, validation, and concurrency tests for the hot-swappable
efficiency-bonus settings admin endpoint."""

from __future__ import annotations

from collections.abc import AsyncIterator
from dataclasses import replace
from typing import Any

import httpx
import pytest
from fastapi import FastAPI
from sqlalchemy.ext.asyncio import (
    AsyncSession,
    async_sessionmaker,
)

from ditto.api_server.dependencies import get_session

_ADMIN_TOKEN = "test-admin-token-at-least-32-characters"
_ADMIN_HEADERS = {"Authorization": f"Bearer {_ADMIN_TOKEN}"}
_URL = "/api/v1/admin/efficiency-bonus-settings"


@pytest.fixture
def settings_maker(
    session_maker: async_sessionmaker[AsyncSession],
) -> async_sessionmaker[AsyncSession]:
    """Local alias for the root Postgres ``session_maker``."""
    return session_maker


def _install(app: FastAPI, maker: async_sessionmaker[AsyncSession]) -> None:
    app.state.config = replace(app.state.config, admin_api_token=_ADMIN_TOKEN)

    async def _session() -> AsyncIterator[AsyncSession]:
        async with maker() as session:
            yield session

    app.dependency_overrides[get_session] = _session


def _settings(**overrides: Any) -> dict[str, Any]:
    base: dict[str, Any] = {
        "enabled": True,
        "fold_enabled": False,
        "cap": 0.05,
        "deep_cap": 0.10,
        "deep_frontier_ratio": 0.5,
        "factor_alpha": 0.25,
        "minimum_factor": 0.85,
        "maximum_factor": 1.10,
        "cohort_size": 25,
        "min_cohort": 8,
        "epoch_hours": 24,
        "quality_floor": 0.0,
        "memory_floor": 0.0,
    }
    base.update(overrides)
    return base


def _payload(
    *, expected: int = 0, confirmation: str | None = None, **settings: Any
) -> dict[str, Any]:
    body = _settings(**settings)
    enabled = body["enabled"]
    return {
        "scope": "*",
        "expected_revision": expected,
        "settings": body,
        "reason": "enable the efficiency bonus for canary",
        "actor": "backroom:test",
        "confirmation": confirmation
        if confirmation is not None
        else f"APPLY EFFICIENCY BONUS {'ENABLED' if enabled else 'DISABLED'}",
    }


class TestAuth:
    async def test_get_requires_admin_token(
        self,
        app: FastAPI,
        client: httpx.AsyncClient,
        settings_maker: async_sessionmaker[AsyncSession],
    ) -> None:
        _install(app, settings_maker)
        assert (await client.get(_URL)).status_code == 401

    async def test_post_requires_admin_token(
        self,
        app: FastAPI,
        client: httpx.AsyncClient,
        settings_maker: async_sessionmaker[AsyncSession],
    ) -> None:
        _install(app, settings_maker)
        assert (await client.post(_URL, json=_payload())).status_code == 401


class TestSeedAndRoundTrip:
    async def test_empty_reports_seed_default(
        self,
        app: FastAPI,
        client: httpx.AsyncClient,
        settings_maker: async_sessionmaker[AsyncSession],
    ) -> None:
        _install(app, settings_maker)
        body = (await client.get(_URL, headers=_ADMIN_HEADERS)).json()
        assert body["current"] == []
        assert body["history"] == []
        assert body["seed_default"]["enabled"] is False
        assert body["effective"]["source"] == "seed"
        assert body["effective"]["revision"] == 0
        assert body["effective"]["fold_effective"] is False

    async def test_apply_then_get_reflects_revision(
        self,
        app: FastAPI,
        client: httpx.AsyncClient,
        settings_maker: async_sessionmaker[AsyncSession],
    ) -> None:
        _install(app, settings_maker)
        created = await client.post(
            _URL, headers=_ADMIN_HEADERS, json=_payload(cap=0.06)
        )
        assert created.status_code == 200, created.text
        assert created.json()["revision"] == 1

        body = (await client.get(_URL, headers=_ADMIN_HEADERS)).json()
        assert len(body["current"]) == 1
        assert body["current"][0]["revision"] == 1
        assert body["effective"]["source"] == "revision"
        assert body["effective"]["settings"]["enabled"] is True
        assert body["effective"]["settings"]["cap"] == 0.06

    async def test_second_revision_chains(
        self,
        app: FastAPI,
        client: httpx.AsyncClient,
        settings_maker: async_sessionmaker[AsyncSession],
    ) -> None:
        _install(app, settings_maker)
        await client.post(_URL, headers=_ADMIN_HEADERS, json=_payload())
        second = await client.post(
            _URL,
            headers=_ADMIN_HEADERS,
            json=_payload(expected=1, fold_enabled=True),
        )
        assert second.status_code == 200, second.text
        assert second.json()["revision"] == 2
        assert second.json()["parent_revision"] == 1

    async def test_epoch_duration_is_immutable_across_revisions(
        self,
        app: FastAPI,
        client: httpx.AsyncClient,
        settings_maker: async_sessionmaker[AsyncSession],
    ) -> None:
        _install(app, settings_maker)
        first = await client.post(_URL, headers=_ADMIN_HEADERS, json=_payload())
        assert first.status_code == 200, first.text

        changed = await client.post(
            _URL,
            headers=_ADMIN_HEADERS,
            json=_payload(expected=1, epoch_hours=12),
        )

        assert changed.status_code == 409
        assert "epoch_hours is immutable" in changed.json()["message"]

    async def test_first_revision_must_keep_seed_epoch_duration(
        self,
        app: FastAPI,
        client: httpx.AsyncClient,
        settings_maker: async_sessionmaker[AsyncSession],
    ) -> None:
        _install(app, settings_maker)

        changed = await client.post(
            _URL,
            headers=_ADMIN_HEADERS,
            json=_payload(epoch_hours=12),
        )

        assert changed.status_code == 409
        assert "deployment seed" in changed.json()["message"]


class TestConfirmationAndConcurrency:
    async def test_wrong_confirmation_is_409(
        self,
        app: FastAPI,
        client: httpx.AsyncClient,
        settings_maker: async_sessionmaker[AsyncSession],
    ) -> None:
        _install(app, settings_maker)
        resp = await client.post(
            _URL,
            headers=_ADMIN_HEADERS,
            json=_payload(confirmation="APPLY EFFICIENCY BONUS"),
        )
        assert resp.status_code == 409
        assert "confirmation" in resp.json()["message"]

    async def test_stale_expected_revision_is_409(
        self,
        app: FastAPI,
        client: httpx.AsyncClient,
        settings_maker: async_sessionmaker[AsyncSession],
    ) -> None:
        _install(app, settings_maker)
        await client.post(_URL, headers=_ADMIN_HEADERS, json=_payload())
        # A second writer still believes revision 0 is current.
        stale = await client.post(_URL, headers=_ADMIN_HEADERS, json=_payload())
        assert stale.status_code == 409


class TestValidation:
    async def test_partial_write_cannot_reset_existing_policy(
        self,
        app: FastAPI,
        client: httpx.AsyncClient,
        settings_maker: async_sessionmaker[AsyncSession],
    ) -> None:
        _install(app, settings_maker)
        payload = _payload()
        payload["settings"] = {
            "factor_alpha": 0.25,
            "minimum_factor": 0.85,
            "maximum_factor": 1.10,
        }

        resp = await client.post(_URL, headers=_ADMIN_HEADERS, json=payload)

        assert resp.status_code == 422

    async def test_legacy_write_shape_cannot_default_v3_authority(
        self,
        app: FastAPI,
        client: httpx.AsyncClient,
        settings_maker: async_sessionmaker[AsyncSession],
    ) -> None:
        _install(app, settings_maker)
        payload = _payload()
        for field in ("factor_alpha", "minimum_factor", "maximum_factor"):
            payload["settings"].pop(field)

        resp = await client.post(_URL, headers=_ADMIN_HEADERS, json=payload)

        assert resp.status_code == 422

    async def test_non_global_scope_rejected(
        self,
        app: FastAPI,
        client: httpx.AsyncClient,
        settings_maker: async_sessionmaker[AsyncSession],
    ) -> None:
        _install(app, settings_maker)
        payload = _payload()
        payload["scope"] = "board-7"
        resp = await client.post(_URL, headers=_ADMIN_HEADERS, json=payload)
        assert resp.status_code == 422

    @pytest.mark.parametrize(
        "overrides",
        [
            {"cap": 0.2},  # cap > 0.10
            {"cap": 0.0},  # cap not > 0
            {"cap": 0.08, "deep_cap": 0.06},  # cap > deep_cap
            {"deep_frontier_ratio": 1.0},  # ratio not < 1
            {"cohort_size": 4, "min_cohort": 8},  # cohort_size < min_cohort
            {"min_cohort": 1},  # min_cohort < 2
            {"quality_floor": 1.5},  # floor out of [0, 1]
            {"epoch_hours": 0},  # epoch_hours < 1
        ],
    )
    async def test_out_of_envelope_knobs_rejected(
        self,
        app: FastAPI,
        client: httpx.AsyncClient,
        settings_maker: async_sessionmaker[AsyncSession],
        overrides: dict[str, Any],
    ) -> None:
        _install(app, settings_maker)
        resp = await client.post(
            _URL, headers=_ADMIN_HEADERS, json=_payload(**overrides)
        )
        assert resp.status_code == 422, resp.text
