"""Tests for redacted Coding control-plane status."""

from __future__ import annotations

from collections.abc import AsyncIterator
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from typing import cast

import httpx
import pytest
from fastapi import FastAPI
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from ditto.api_server.dependencies import get_session
from ditto.api_server.endpoints.admin_coding_control_plane import _state
from ditto.db.models import CodingHostedAssignment, CodingHostedPrivateTask
from ditto.tests.db.queries.test_coding_hosted_admission import _admit, _request, _seed

_ADMIN_TOKEN = "test-admin-token-at-least-32-characters"
_HEADERS = {"Authorization": f"Bearer {_ADMIN_TOKEN}"}


def test_control_plane_expiry_precedes_nonterminal_durable_markers() -> None:
    now = datetime.now(UTC)
    expired = cast(
        CodingHostedAssignment,
        SimpleNamespace(
            expires_at=now - timedelta(seconds=1),
            admitted_at=now - timedelta(minutes=2),
            started_at=now - timedelta(minutes=1),
        ),
    )
    assert _state(expired, None, now=now) == "expired"

    closed = cast(
        CodingHostedPrivateTask,
        SimpleNamespace(closed_at=now, close_reason="completed"),
    )
    assert _state(expired, closed, now=now) == "completed"


def _install(app: FastAPI, maker: async_sessionmaker[AsyncSession]) -> None:
    app.state.config = replace(
        app.state.config,
        admin_api_token=_ADMIN_TOKEN,
        coding_shadow_reconciliation_enabled=True,
        coding_shadow_ticket_set_enabled=True,
        coding_shadow_ticket_lease_seconds=3600,
    )

    async def _session() -> AsyncIterator[AsyncSession]:
        async with maker() as session:
            yield session

    app.dependency_overrides[get_session] = _session


@pytest.mark.asyncio
async def test_control_plane_reports_redacted_native_progress_and_feature_gates(
    app: FastAPI,
    client: httpx.AsyncClient,
    session_maker: async_sessionmaker[AsyncSession],
) -> None:
    _install(app, session_maker)
    authority = await _seed(session_maker)
    url = "/api/v1/admin/coding-control-plane"
    assert (await client.get(url)).status_code == 401

    pending = await client.get(url, headers=_HEADERS)
    assert pending.status_code == 200
    assert pending.headers["cache-control"] == "no-store"
    payload = pending.json()
    assert payload["total_native_operations"] == 1
    assert payload["contract_v1_reconciliation_enabled"] is True
    assert payload["contract_v1_ticket_set_enabled"] is True
    assert payload["contract_v1_ticket_lease_seconds"] == 3600
    assert payload["native_v2_selectable"] is False
    assert payload["shadow_only"] is True
    assert payload["weight_eligible"] is False
    operation = payload["native_operations"][0]
    assert operation["evaluation_id"] == str(authority.evaluation_id)
    assert operation["assignment_sha256"] == authority.digest()
    assert operation["state"] == "pending_admission"
    assert operation["registered_actor"] == "test-operator"
    assert operation["registered_reason"] == "synthetic shadow approval"
    assert operation["frozen"] is False
    assert operation["closed_at"] is None
    serialized = pending.text
    for forbidden in (
        "catalog_index",
        "selection_authority",
        "authoring_grant_id",
        "grading_grant_id",
        "frozen_patch_sha256",
    ):
        assert forbidden not in serialized

    await _admit(session_maker, _request(authority))
    admitted = await client.get(url, headers=_HEADERS)
    assert admitted.status_code == 200
    assert admitted.json()["native_operations"][0]["state"] == "admitted"


@pytest.mark.asyncio
async def test_control_plane_limit_is_bounded(
    app: FastAPI,
    client: httpx.AsyncClient,
    session_maker: async_sessionmaker[AsyncSession],
) -> None:
    _install(app, session_maker)
    url = "/api/v1/admin/coding-control-plane"
    for limit in (0, 101):
        response = await client.get(url, params={"limit": limit}, headers=_HEADERS)
        assert response.status_code == 422
