"""Retired private-audit APIs retain authenticated historical reads."""

from __future__ import annotations

from collections.abc import AsyncIterator
from dataclasses import replace

import httpx
import pytest
from fastapi import FastAPI
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from ditto.api_server.dependencies import get_session


def _install(app: FastAPI, maker: async_sessionmaker[AsyncSession]) -> None:
    app.state.config = replace(
        app.state.config,
        admin_api_token="test-admin-token-at-least-32-characters",
    )
    app.state.session_maker = maker

    async def _session() -> AsyncIterator[AsyncSession]:
        async with maker() as session:
            yield session

    app.dependency_overrides[get_session] = _session


pytestmark = pytest.mark.asyncio


async def test_generation_group_rows_are_immutable_in_postgres(
    app: FastAPI,
    session_maker: async_sessionmaker[AsyncSession],
) -> None:
    """The database, not only the admin handler, rejects receipt mutation."""
    _install(app, session_maker)
    # The migration test inspects the trigger even before any live group exists.
    async with session_maker() as session:
        triggers = set(
            await session.scalars(
                text(
                    "SELECT tgname FROM pg_trigger WHERE NOT tgisinternal "
                    "AND tgname LIKE 'v13_%_immutable'"
                )
            )
        )
    assert "v13_private_generation_groups_immutable" in triggers
    assert "v13_known_benign_control_approvals_immutable" in triggers
    assert "v13_group_package_registrations_immutable" in triggers


async def test_legacy_private_reads_require_admin_and_cannot_start_generation(
    app: FastAPI,
    client: httpx.AsyncClient,
    session_maker: async_sessionmaker[AsyncSession],
) -> None:
    _install(app, session_maker)
    base = "/api/v1/admin/v13-private-generation"
    headers = {"Authorization": "Bearer test-admin-token-at-least-32-characters"}
    assert (await client.get(base + "/known-benign-approvals")).status_code == 401
    response = await client.get(base + "/known-benign-approvals", headers=headers)
    assert response.status_code == 200
    assert response.json() == []
    assert (
        await client.post(base + "/known-benign-approvals", json={}, headers=headers)
    ).status_code == 405
    assert (
        await client.post(base + "/groups", json={}, headers=headers)
    ).status_code == 404


async def test_private_execution_routes_are_absent_from_openapi(app: FastAPI) -> None:
    paths = app.openapi()["paths"]
    for path, operations in paths.items():
        if "/v13-private-generation" in path or "/v13-official-controls" in path:
            assert set(operations) <= {"get"}, path
        assert not path.endswith("/private-package-registration"), path
        assert not path.endswith("/private-inputs"), path
        if path.endswith("/private-receipt"):
            assert set(operations) == {"get"}
